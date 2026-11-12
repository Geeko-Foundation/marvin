from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from payments.models import PaymentRecord
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP
from tsp.models import Event, Expense, RequestState, TravelRequest
from tsp.tests import make_event, make_requester, make_user


class ReviewTests(TestCase):
    """What the committee approves has to make sense, and come from someone else."""

    def setUp(self):
        self.owner = make_user('reviewowner', [GROUP_REQUESTER])
        self.chair = make_user('reviewchair', [GROUP_TSP])
        self.event = make_event()

    def submitted(self, user=None, event=None, estimate=100):
        req = TravelRequest.objects.create(
            user=user or self.owner, event=event or self.event,
            state=RequestState.SUBMITTED, description='T')
        Expense.objects.create(
            request=req, category='flight', description='Flight',
            estimated_amount=estimate, estimated_currency='EUR')
        return req

    def approve(self, req, amount, currency='EUR', as_user=None, **extra):
        self.client.force_login(as_user or self.chair)
        exp = req.expenses.first()
        return self.client.post(reverse('request_approve', args=[req.pk]), {
            'notes': 'ok', 'expense_id': [str(exp.pk)],
            f'approved_amount_{exp.pk}': amount, f'approved_currency_{exp.pk}': currency, **extra})

    def assert_untouched(self, req):
        req.refresh_from_db()
        self.assertEqual(req.state, RequestState.SUBMITTED)
        self.assertIsNone(req.expenses.first().approved_amount)

    def test_a_negative_approval_is_refused_and_saves_nothing(self):
        req = self.submitted()
        self.approve(req, '-500')
        self.assert_untouched(req)

    def test_a_currency_not_offered_is_refused(self):
        for currency in ('ZZZ', 'abc<script>'):
            with self.subTest(currency=currency):
                req = self.submitted()
                self.approve(req, '100', currency)
                self.assert_untouched(req)

    def test_a_refused_over_budget_approval_leaves_the_budget_alone(self):
        event = Event.objects.create(
            name='Budgeted', location='X', budget=Decimal('100'), budget_currency='EUR',
            start_date=date(2099, 1, 1), end_date=date(2099, 1, 2))
        big = self.submitted(event=event, estimate=500)
        self.approve(big, '500')
        self.assert_untouched(big)
        self.assertEqual(event.budget_remaining(), Decimal('100'))

        small = self.submitted(event=event, estimate=50)
        self.approve(small, '50')
        small.refresh_from_db()
        self.assertEqual(small.state, RequestState.APPROVED)

    def test_approving_a_request_that_has_moved_on_changes_no_amounts(self):
        req = self.submitted()
        req.state = RequestState.PAID
        req.save()
        exp = req.expenses.first()
        exp.approved_amount, exp.approved_currency = Decimal('100'), 'EUR'
        exp.save()
        self.approve(req, '999')
        exp.refresh_from_db()
        self.assertEqual(exp.approved_amount, Decimal('100'))

    def test_a_committee_member_cannot_approve_their_own_request(self):
        both = make_user('reviewboth', [GROUP_REQUESTER, GROUP_TSP])
        req = self.submitted(user=both)
        self.approve(req, '100', as_user=both)
        self.assert_untouched(req)

    def test_a_committee_member_cannot_reject_their_own_request(self):
        both = make_user('rejectboth', [GROUP_REQUESTER, GROUP_TSP])
        req = self.submitted(user=both)
        self.client.force_login(both)
        self.client.post(reverse('request_reject', args=[req.pk]), {'notes': 'no'})
        req.refresh_from_db()
        self.assertEqual(req.state, RequestState.SUBMITTED)

    def test_a_batch_skips_the_reviewers_own_requests(self):
        both = make_user('batchboth', [GROUP_REQUESTER, GROUP_TSP])
        own = self.submitted(user=both)
        other = self.submitted()
        self.client.force_login(both)
        self.client.post(reverse('request_batch_review'), {
            'action': 'approve', 'request_ids': [own.pk, other.pk]})
        own.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(own.state, RequestState.SUBMITTED)
        self.assertEqual(other.state, RequestState.APPROVED)

    def test_a_batch_does_not_approve_past_the_budget(self):
        event = Event.objects.create(
            name='Tight', location='X', budget=Decimal('100'), budget_currency='EUR',
            start_date=date(2099, 1, 1), end_date=date(2099, 1, 2))
        fits = self.submitted(event=event, estimate=80)
        over = self.submitted(event=event, estimate=80)
        self.client.force_login(self.chair)
        self.client.post(reverse('request_batch_review'), {
            'action': 'approve', 'request_ids': [fits.pk, over.pk]})
        states = sorted(TravelRequest.objects.filter(pk__in=[fits.pk, over.pk])
                        .values_list('state', flat=True))
        self.assertEqual(states, [RequestState.APPROVED, RequestState.SUBMITTED])
        self.assertEqual(event.budget_remaining(), Decimal('20'))


class AmountValidationTests(TestCase):
    """Money fields refuse values that make no sense, not just in the browser."""

    def setUp(self):
        self.owner = make_user('amountowner', [GROUP_REQUESTER])
        self.event = make_event()

    def test_a_request_cannot_estimate_zero_or_less(self):
        self.client.force_login(self.owner)
        for amount in ('-50', '0'):
            with self.subTest(amount=amount):
                self.client.post(reverse('request_create'), {
                    'event': self.event.pk, 'description': 'T',
                    'expenses-TOTAL_FORMS': '1', 'expenses-INITIAL_FORMS': '0',
                    'expenses-MIN_NUM_FORMS': '1', 'expenses-MAX_NUM_FORMS': '1000',
                    'expenses-0-category': 'flight', 'expenses-0-description': 'Flight',
                    'expenses-0-estimated_amount': amount, 'expenses-0-estimated_currency': 'EUR'})
                self.assertFalse(Expense.objects.filter(estimated_amount=Decimal(amount)).exists())

    def test_an_actual_cost_cannot_be_negative(self):
        req = TravelRequest.objects.create(
            user=self.owner, event=self.event, state=RequestState.ACCEPTED, description='T')
        exp = Expense.objects.create(
            request=req, category='flight', description='Flight',
            estimated_amount=100, estimated_currency='EUR',
            approved_amount=100, approved_currency='EUR')
        self.client.force_login(self.owner)
        self.client.post(reverse('request_actuals', args=[req.pk]), {
            'expenses-TOTAL_FORMS': '1', 'expenses-INITIAL_FORMS': '1',
            'expenses-MIN_NUM_FORMS': '0', 'expenses-MAX_NUM_FORMS': '1000',
            'expenses-0-id': str(exp.pk), 'expenses-0-actual_amount': '-40',
            'expenses-0-actual_currency': 'EUR'})
        exp.refresh_from_db()
        self.assertIsNone(exp.actual_amount)


class EventRulesTests(TestCase):
    """Who may create events, and which events still take requests."""

    def setUp(self):
        self.owner = make_user('eventowner', [GROUP_REQUESTER])
        self.chair = make_user('eventchair', [GROUP_TSP])

    def test_only_the_committee_can_create_events(self):
        payload = {'name': 'Rogue', 'location': 'X', 'start_date': '2099-01-01',
                   'end_date': '2099-01-02', 'accepting_requests': 'on'}
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse('event_create'), payload).status_code, 403)
        self.assertFalse(Event.objects.filter(name='Rogue').exists())
        self.assertNotContains(self.client.get(reverse('event_list')), 'New Event')

    def test_an_event_cannot_end_before_it_starts(self):
        self.client.force_login(self.chair)
        self.client.post(reverse('event_create'), {
            'name': 'Backwards', 'location': 'X', 'start_date': '2099-01-05',
            'end_date': '2099-01-01', 'accepting_requests': 'on'})
        self.assertFalse(Event.objects.filter(name='Backwards').exists())

    def test_closed_events_cannot_be_requested_for(self):
        closed = [
            Event.objects.create(name='Past', location='X',
                                 start_date=date(2020, 1, 1), end_date=date(2020, 1, 2)),
            Event.objects.create(name='Off', location='X', accepting_requests=False,
                                 start_date=date(2099, 1, 1), end_date=date(2099, 1, 2)),
            Event.objects.create(name='Lapsed', location='X', requests_open_until=date(2020, 1, 1),
                                 start_date=date(2099, 1, 1), end_date=date(2099, 1, 2)),
        ]
        self.client.force_login(self.owner)
        for event in closed:
            with self.subTest(event=event.name):
                self.client.post(reverse('request_create'), {
                    'event': event.pk, 'description': 'T',
                    'expenses-TOTAL_FORMS': '1', 'expenses-INITIAL_FORMS': '0',
                    'expenses-MIN_NUM_FORMS': '1', 'expenses-MAX_NUM_FORMS': '1000',
                    'expenses-0-category': 'flight', 'expenses-0-description': 'Flight',
                    'expenses-0-estimated_amount': '100', 'expenses-0-estimated_currency': 'EUR'})
                self.assertFalse(TravelRequest.objects.filter(event=event).exists())

    def test_open_for_requests_agrees_with_the_queryset(self):
        events = [
            Event.objects.create(name=f'E{i}', location='X', **kw)
            for i, kw in enumerate([
                dict(start_date=date(2099, 1, 1), end_date=date(2099, 1, 2)),
                dict(start_date=date(2020, 1, 1), end_date=date(2020, 1, 2)),
                dict(accepting_requests=False, start_date=date(2099, 1, 1), end_date=date(2099, 1, 2)),
                dict(requests_open_until=date(2020, 1, 1), start_date=date(2099, 1, 1), end_date=date(2099, 1, 2)),
                dict(requests_open_until=date(2099, 1, 1), start_date=date(2099, 2, 1), end_date=date(2099, 2, 2)),
            ])
        ]
        in_queryset = set(Event.open_for_requests().values_list('pk', flat=True))
        for event in events:
            self.assertEqual(event.pk in in_queryset, event.is_open_for_requests(), event.name)


class DashboardTotalsTests(TestCase):
    """Totals keep their currencies apart and count only money that moved."""

    def test_approved_funds_are_shown_per_currency(self):
        owner = make_user('totalsowner', [GROUP_REQUESTER])
        chair = make_user('totalschair', [GROUP_TSP])
        event = make_event()
        for currency, amount in (('INR', 9000), ('EUR', 90)):
            req = TravelRequest.objects.create(
                user=owner, event=event, state=RequestState.APPROVED, description='T')
            Expense.objects.create(
                request=req, category='flight', description='F',
                estimated_amount=amount, estimated_currency=currency,
                approved_amount=amount, approved_currency=currency)
        self.client.force_login(chair)
        page = self.client.get(reverse('dashboard'))
        self.assertContains(page, '9,000 INR')
        self.assertContains(page, '90 EUR')
        self.assertNotContains(page, '€')

    def test_failed_transfers_do_not_count_as_paid(self):
        owner = make_user('paidowner', [GROUP_REQUESTER])
        finance = make_user('paidfinance', [GROUP_FINANCE])
        req = TravelRequest.objects.create(
            user=owner, event=make_event(), state=RequestState.PAID, description='T')
        for status, amount in (('completed', 40), ('failed', 500)):
            PaymentRecord.objects.create(
                content_object=req, gateway_id='manual', amount=amount,
                currency='EUR', status=status, processed_by=finance)
        self.client.force_login(finance)
        page = self.client.get(reverse('dashboard'))
        self.assertContains(page, '40 EUR')
        self.assertNotContains(page, '540')


class UserSearchTests(TestCase):
    def test_staff_search_does_not_show_password_hashes(self):
        staff = make_user('searchstaff', [GROUP_TSP])
        staff.is_staff = True
        staff.save()
        make_user('searchtarget', [GROUP_REQUESTER])
        self.client.force_login(staff)
        page = self.client.get(reverse('search'), {'q': 'searchtarget'})
        self.assertContains(page, 'searchtarget')
        self.assertNotContains(page, 'pbkdf2_')
        self.assertNotContains(page, 'Password')


class DocumentUploadTests(TestCase):
    """Only the requester adds evidence, and only while the claim is theirs to build."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.file = lambda: SimpleUploadedFile('receipt.pdf', b'%PDF-1.4 receipt')
        self.owner = make_user('uploadowner', [GROUP_REQUESTER])
        self.req = TravelRequest.objects.create(
            user=self.owner, event=make_event(), state=RequestState.ACCEPTED, description='T')

    def upload(self, user):
        from tsp.models import Document
        self.client.force_login(user)
        response = self.client.post(reverse('request_document_upload', args=[self.req.pk]),
                                    {'kind': 'receipt', 'file': self.file()})
        return response, Document.objects.filter(request=self.req).count()

    def test_the_requester_can_upload_while_accepted(self):
        _, count = self.upload(self.owner)
        self.assertEqual(count, 1)

    def test_committee_and_finance_cannot_upload_to_someone_elses_request(self):
        for group in (GROUP_TSP, GROUP_FINANCE):
            with self.subTest(group=group):
                response, count = self.upload(make_user(f'uploader-{group}', [group]))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(count, 0)

    def test_nothing_can_be_added_once_the_request_has_moved_on(self):
        for state in (RequestState.SUBMITTED, RequestState.SUBMITTED_FOR_PAYMENT,
                      RequestState.PAID, RequestState.CANCELLED):
            with self.subTest(state=state):
                self.req.state = state
                self.req.save()
                _, count = self.upload(self.owner)
                self.assertEqual(count, 0)

    def test_staff_do_not_see_the_upload_form(self):
        self.client.force_login(make_user('uploadviewer', [GROUP_TSP]))
        page = self.client.get(reverse('request_detail', args=[self.req.pk]))
        self.assertNotContains(page, reverse('request_document_upload', args=[self.req.pk]))


class HistoryVisibilityTests(TestCase):
    """Requesters see what happened and which team did it; staff see who."""

    def setUp(self):
        from tsp.models import StateHistory
        self.owner = make_user('historyowner', [GROUP_REQUESTER])
        self.finance = make_user('historyfinance', [GROUP_FINANCE])
        self.req = TravelRequest.objects.create(
            user=self.owner, event=make_event(), state=RequestState.PAID, description='T')
        StateHistory.objects.create(
            request=self.req, user=self.finance, from_state='approved', to_state='accepted',
            notes='Fine [over budget by 40 EUR]')
        StateHistory.objects.create(
            request=self.req, user=self.finance, from_state='submitted_for_payment',
            to_state='paid', notes='Payment via Wise Batch CSV')

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('request_detail', args=[self.req.pk])).content.decode()

    def test_the_requester_sees_the_team_not_the_username_or_internal_notes(self):
        html = self.page(self.owner)
        self.assertNotIn('historyfinance', html)
        self.assertIn('Finance', html)
        self.assertNotIn('Travel committee', html)
        self.assertNotIn('over budget', html)
        self.assertIn('Paid via Wise', html)
        self.assertNotIn('Wise Batch CSV', html)

    def test_staff_still_see_who_did_it(self):
        html = self.page(make_user('historytsp', [GROUP_TSP]))
        self.assertIn('historyfinance', html)
        self.assertIn('over budget by 40 EUR', html)


class ApprovalShareTests(TestCase):
    """An event that covers part of each request starts every review there."""

    def setUp(self):
        self.event = make_event()
        self.event.approval_percent = 69
        self.event.save()
        self.owner = make_user('shareowner', [GROUP_REQUESTER])
        self.committee = make_user('sharecommittee', [GROUP_TSP])
        self.req = TravelRequest.objects.create(
            user=self.owner, event=self.event, state=RequestState.SUBMITTED, description='T')
        self.expense = Expense.objects.create(
            request=self.req, category='flight', description='F',
            estimated_amount=Decimal('200'), estimated_currency='EUR')

    def test_the_review_form_starts_at_the_events_share(self):
        self.client.force_login(self.committee)
        html = self.client.get(reverse('request_detail', args=[self.req.pk])).content.decode()
        self.assertIn('value="138.00"', html)
        self.assertIn('data-full="200', html)
        self.assertIn('value="69"', html)

    def test_batch_approval_applies_the_share(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('request_batch_review'), {'request_ids': [self.req.pk], 'action': 'approve'})
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.approved_amount, Decimal('138.00'))

    def test_the_share_cannot_exceed_everything(self):
        from django.core.exceptions import ValidationError
        self.event.approval_percent = 101
        with self.assertRaises(ValidationError):
            self.event.full_clean()


class PaymentSummaryTests(TestCase):
    """The requester sees what Wise charged and what reached their account."""

    def test_fee_and_conversion_are_shown_to_the_requester(self):
        from django.contrib.contenttypes.models import ContentType
        owner = make_user('summaryowner', [GROUP_REQUESTER])
        req = TravelRequest.objects.create(
            user=owner, event=make_event(), state=RequestState.PAID, description='T')
        PaymentRecord.objects.create(
            content_type=ContentType.objects.get_for_model(req), object_id=req.pk,
            gateway_id='wise-api', amount=Decimal('10.00'), currency='EUR', status='completed',
            external_id='2149871408',
            reference=req.reference, recipient_name='x', recipient_email='x@example.org',
            metadata={'fee': '0.57', 'fee_currency': 'EUR', 'exchange_rate': '103.722',
                      'target_amount': '978.09', 'target_currency': 'INR'})
        self.client.force_login(owner)
        html = self.client.get(reverse('request_detail', args=[req.pk])).content.decode()
        self.assertIn("Wise's fee of 0.57 EUR was paid by the foundation.", html)
        self.assertIn('978.09 INR reaches the account', html)
        self.assertIn(f'Wise transfer 2149871408 · reference {req.reference}', html)


class ApprovalPercentRecordTests(TestCase):
    """The share the committee approved at stays on the request for everyone to see."""

    setUp = ApprovalShareTests.setUp

    def test_the_share_chosen_on_the_form_is_stored_and_shown(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[self.req.pk]), {
            'expense_id': [str(self.expense.pk)],
            f'approved_amount_{self.expense.pk}': '160.00',
            f'approved_currency_{self.expense.pk}': 'EUR',
            'approval_percent': '80',
        })
        self.req.refresh_from_db()
        self.assertEqual((self.req.state, self.req.approval_percent), (RequestState.APPROVED, 80))
        self.client.force_login(self.owner)
        html = self.client.get(reverse('request_detail', args=[self.req.pk])).content.decode()
        self.assertIn('Approved at 80% of what was asked for.', html)

    def test_batch_approval_records_the_events_share(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('request_batch_review'), {'request_ids': [self.req.pk], 'action': 'approve'})
        self.req.refresh_from_db()
        self.assertEqual(self.req.approval_percent, 69)


class AuditLogPageTests(TestCase):
    """Committee and finance can read the trail; requesters cannot."""

    def setUp(self):
        from profile.audit_log import record
        self.finance = make_user('auditfinance', [GROUP_FINANCE])
        record('payment.processed', user=self.finance, target='wise-api TSP9', detail='10.00 EUR')
        record('event.updated', user=self.finance, target='Asia Summit', detail='budget')

    def test_staff_see_entries_and_can_search_them(self):
        self.client.force_login(self.finance)
        html = self.client.get(reverse('audit_log')).content.decode()
        self.assertIn('wise-api TSP9', html)
        self.assertIn('Asia Summit', html)
        html = self.client.get(reverse('audit_log'), {'q': 'TSP9'}).content.decode()
        self.assertIn('wise-api TSP9', html)
        self.assertNotIn('Asia Summit', html)

    def test_requesters_are_refused(self):
        self.client.force_login(make_user('auditrequester', [GROUP_REQUESTER]))
        self.assertEqual(self.client.get(reverse('audit_log')).status_code, 403)


class LoginLimitTests(TestCase):
    """Guessing passwords for one account stops after ten wrong tries."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = make_user('lockme', [GROUP_REQUESTER])
        self.user.set_password('right-pass-phrase-9')
        self.user.save()

    def attempt(self, password, **extra):
        return self.client.post(reverse('login'), {'username': 'lockme', 'password': password, **extra})

    def test_the_account_locks_after_ten_failures_even_for_the_right_password(self):
        for _ in range(10):
            self.attempt('wrong')
        response = self.attempt('right-pass-phrase-9')
        self.assertContains(response, 'Too many failed sign-in attempts')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signing_in_clears_earlier_failures(self):
        for _ in range(9):
            self.attempt('wrong')
        self.assertEqual(self.attempt('right-pass-phrase-9').status_code, 302)
        self.client.logout()
        for _ in range(9):
            self.attempt('wrong')
        self.assertEqual(self.attempt('right-pass-phrase-9').status_code, 302)

    def test_next_never_leads_off_the_site(self):
        response = self.attempt('right-pass-phrase-9', next='https://evil.example/phish')
        self.assertEqual(response['Location'], reverse('dashboard'))
        self.client.logout()
        response = self.attempt('right-pass-phrase-9', next='/events/')
        self.assertEqual(response['Location'], '/events/')


class ReportQueryTests(TestCase):
    """The reports page asks the database the same amount however many rows it shows."""

    def setUp(self):
        self.finance = make_user('reportsfinance', [GROUP_FINANCE])
        self.event = make_event()

    def add_requests(self, count):
        from django.contrib.contenttypes.models import ContentType
        for i in range(count):
            owner = make_user(f'reportowner{TravelRequest.objects.count()}', [GROUP_REQUESTER])
            req = TravelRequest.objects.create(user=owner, event=self.event, state=RequestState.PAID, description='T')
            Expense.objects.create(request=req, category='flight', description='F', estimated_amount=10,
                                   estimated_currency='EUR', approved_amount=10, approved_currency='EUR')
            PaymentRecord.objects.create(
                content_type=ContentType.objects.get_for_model(req), object_id=req.pk, gateway_id='manual',
                amount=10, currency='EUR', status='completed', reference=req.reference,
                recipient_name='x', recipient_email='x@example.org')

    def queries_for_csv(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.force_login(self.finance)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(reverse('reports'), {'format': 'csv'}).status_code, 200)
        return len(captured)

    def test_more_rows_do_not_mean_more_queries(self):
        self.add_requests(2)
        few = self.queries_for_csv()
        self.add_requests(8)
        self.assertEqual(self.queries_for_csv(), few)


class EventCategoryTests(TestCase):
    """An event can cover costs the policy leaves out, and requesters see exactly those."""

    def setUp(self):
        self.committee = make_user('categorycommittee', [GROUP_TSP])
        self.owner = make_requester('categoryowner')
        self.event = make_event()

    def save_event(self, **extra):
        self.client.force_login(self.committee)
        self.client.post(reverse('event_edit', args=[self.event.pk]), {
            'name': self.event.name, 'location': 'Prague', 'start_date': '2027-06-14',
            'end_date': '2027-06-16', 'approval_percent': '100', 'accepting_requests': 'on', **extra})
        self.event.refresh_from_db()

    def test_ticked_and_custom_categories_are_stored(self):
        self.save_event(offered_categories=['visa'], custom_categories='Childcare,  childcare , Flight,')
        self.assertEqual(self.event.extra_expense_categories, ['visa', 'Childcare'])
        self.assertIn(('visa', 'Visa fees'), self.event.expense_categories())

    def test_a_category_name_cannot_carry_markup(self):
        self.save_event(custom_categories='<script>')
        self.assertEqual(self.event.extra_expense_categories, [])

    def request_with(self, category):
        self.client.force_login(self.owner)
        self.client.post(reverse('request_create'), {
            'event': self.event.pk, 'description': 'Talk',
            'expenses-TOTAL_FORMS': '1', 'expenses-INITIAL_FORMS': '0',
            'expenses-0-category': category, 'expenses-0-description': 'Fee',
            'expenses-0-estimated_amount': '60', 'expenses-0-estimated_currency': 'EUR',
        })
        return Expense.objects.filter(request__user=self.owner, category=category).first()

    def test_requesters_can_claim_what_the_event_covers_and_nothing_else(self):
        self.assertIsNone(self.request_with('visa'))
        self.save_event(offered_categories=['visa'], custom_categories='Childcare')
        self.assertEqual(self.request_with('visa').get_category_display(), 'Visa fees')
        self.assertEqual(self.request_with('Childcare').get_category_display(), 'Childcare')
