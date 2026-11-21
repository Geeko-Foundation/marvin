"""Changes asked for in the 19 November 2026 walkthrough."""

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from payments.models import PaymentRecord
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP
from tsp.models import RequestState
from tsp.test_advances import make_request
from tsp.tests import make_user


class FinanceCancelTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.finance = make_user('counter', [GROUP_FINANCE])
        self.client.force_login(self.finance)

    def cancel(self, travel_request):
        self.client.post(reverse('request_cancel', args=[travel_request.pk]))
        travel_request.refresh_from_db()
        return travel_request.state

    def test_finance_can_cancel_a_request_waiting_for_payment(self):
        travel_request = make_request(self.owner, RequestState.SUBMITTED_FOR_PAYMENT)
        self.assertEqual(self.cancel(travel_request), RequestState.CANCELLED)

    def test_finance_cannot_cancel_before_it_reaches_them(self):
        travel_request = make_request(self.owner, RequestState.ACCEPTED)
        response = self.client.post(reverse('request_cancel', args=[travel_request.pk]))
        self.assertEqual(response.status_code, 403)

    def test_a_payment_under_way_stops_the_cancel(self):
        travel_request = make_request(self.owner, RequestState.SUBMITTED_FOR_PAYMENT)
        PaymentRecord.objects.create(
            content_type=ContentType.objects.get_for_model(travel_request), object_id=travel_request.pk,
            gateway_id='wise-api', amount=Decimal('500'), currency='EUR', recipient_name='T',
            recipient_email='t@example.org', reference=travel_request.reference, status='processing',
        )
        self.assertEqual(self.cancel(travel_request), RequestState.SUBMITTED_FOR_PAYMENT)


class ReviewButtonTests(TestCase):

    def test_a_request_under_review_offers_reject_but_not_cancel(self):
        travel_request = make_request(make_user('traveller', [GROUP_REQUESTER]), RequestState.SUBMITTED)
        self.client.force_login(make_user('reviewer', [GROUP_TSP]))
        response = self.client.get(reverse('request_detail', args=[travel_request.pk]))
        self.assertContains(response, 'Reject Request')
        self.assertNotContains(response, 'Cancel Request')


class BulkApproveTests(TestCase):

    def test_approving_everything_selected_says_so(self):
        owner = make_user('traveller', [GROUP_REQUESTER])
        ids = [make_request(owner, RequestState.SUBMITTED).pk for _ in range(2)]
        self.client.force_login(make_user('reviewer', [GROUP_TSP]))
        response = self.client.post(
            reverse('request_batch_review'), {'request_ids': ids, 'action': 'approve'}, follow=True,
        )
        self.assertContains(response, 'All selected requests approved.')


class FinanceDashboardTests(TestCase):

    def test_recently_paid_links_to_the_request(self):
        travel_request = make_request(make_user('traveller', [GROUP_REQUESTER]), RequestState.PAID)
        self.client.force_login(make_user('counter', [GROUP_FINANCE]))
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, reverse('request_detail', args=[travel_request.pk]))


class EventProposalTests(TestCase):

    def propose(self, user, **extra):
        self.client.force_login(user)
        return self.client.post(reverse('event_create'), {
            'name': 'Asia Summit', 'location': 'Tokyo', 'start_date': '2027-08-01',
            'end_date': '2027-08-03', **extra,
        })

    def test_a_requester_proposes_an_event_that_stays_closed(self):
        from tsp.models import Event
        self.propose(make_user('traveller', [GROUP_REQUESTER]), budget='99999', accepting_requests='on',
                     suggested_budget='5000', budget_currency='USD')
        event = Event.objects.get(name='Asia Summit')
        self.assertFalse(event.accepting_requests)
        self.assertIsNone(event.budget)
        self.assertEqual((event.suggested_budget, event.budget_currency), (Decimal('5000'), 'USD'))

    def test_finance_can_propose_too(self):
        response = self.propose(make_user('counter', [GROUP_FINANCE]))
        self.assertRedirects(response, reverse('event_list'), fetch_redirect_response=False)

    def test_the_committee_still_creates_an_open_event(self):
        from tsp.models import Event
        self.propose(make_user('reviewer', [GROUP_TSP]), accepting_requests='on', approval_percent='100',
                     budget_currency='EUR')
        self.assertTrue(Event.objects.get(name='Asia Summit').accepting_requests)


class OrganiserTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.organiser = make_user('organiser', [GROUP_REQUESTER])
        self.request = make_request(self.owner, RequestState.SUBMITTED)
        self.event = self.request.event
        self.event.organizers.add(self.organiser)
        self.client.force_login(self.organiser)

    def approve(self, travel_request, amount='500'):
        expense = travel_request.expenses.get()
        self.client.post(reverse('request_approve', args=[travel_request.pk]), {
            'expense_id': expense.pk, f'approved_amount_{expense.pk}': amount,
            f'approved_currency_{expense.pk}': 'EUR', 'approval_percent': '100',
        })
        travel_request.refresh_from_db()
        return travel_request.state

    def test_an_organiser_reviews_their_events_requests(self):
        response = self.client.get(reverse('request_detail', args=[self.request.pk]))
        self.assertContains(response, 'Organiser review')
        self.assertEqual(self.approve(self.request), RequestState.APPROVED)

    def test_an_organiser_cannot_touch_another_event(self):
        other = make_request(self.owner, RequestState.SUBMITTED)
        other.event.organizers.clear()
        self.assertEqual(self.client.get(reverse('request_detail', args=[other.pk])).status_code, 403)
        self.assertEqual(
            self.client.post(reverse('request_approve', args=[other.pk])).status_code, 403,
        )

    def test_an_organiser_stays_within_the_budget(self):
        self.event.budget, self.event.budget_currency = Decimal('100'), 'EUR'
        self.event.save()
        self.assertEqual(self.approve(self.request), RequestState.SUBMITTED)

    def test_the_role_ends_90_days_after_the_event(self):
        from datetime import date, timedelta
        self.event.start_date = self.event.end_date = date.today() - timedelta(days=91)
        self.event.save()
        self.assertEqual(self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 403)

    def test_an_organiser_cannot_edit_the_event_or_pay(self):
        self.assertEqual(self.client.get(reverse('event_edit', args=[self.event.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('payments:dashboard')).status_code, 403)

    def test_the_dashboard_lists_requests_to_review(self):
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'Events you organise')
        self.assertContains(response, self.request.reference)

    def test_the_committee_picks_organisers_on_the_event(self):
        self.client.force_login(make_user('chair', [GROUP_TSP]))
        event = self.event
        self.client.post(reverse('event_edit', args=[event.pk]), {
            'name': event.name, 'location': event.location, 'start_date': event.start_date,
            'end_date': event.end_date, 'approval_percent': '100', 'budget_currency': 'EUR',
            'accepting_requests': 'on', 'organizers': [self.owner.pk],
        })
        self.assertEqual(list(event.organizers.all()), [self.owner])


class ReimbursementFlowTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.client.force_login(self.owner)

    def test_accepting_goes_home_with_a_friendly_message(self):
        travel_request = make_request(self.owner, RequestState.APPROVED)
        from tsp.forms import AcceptForm
        response = self.client.post(reverse('request_accept', args=[travel_request.pk]),
                                    {name: 'on' for name in AcceptForm.base_fields}, follow=True)
        travel_request.refresh_from_db()
        self.assertEqual(travel_request.state, RequestState.ACCEPTED)
        self.assertRedirects(response, reverse('dashboard'))
        self.assertContains(response, 'Enjoy the event')

    def test_documents_come_after_the_actual_costs(self):
        travel_request = make_request(self.owner, RequestState.ACCEPTED)
        page = self.client.get(reverse('request_detail', args=[travel_request.pk]))
        self.assertContains(page, 'Ask for reimbursement')
        self.assertContains(page, 'once your actual costs are in')
        expense = travel_request.expenses.get()
        expense.actual_amount, expense.actual_currency = Decimal('480'), 'EUR'
        expense.save()
        page = self.client.get(reverse('request_detail', args=[travel_request.pk]))
        self.assertContains(page, 'Print signed form')

    def test_the_signed_form_shows_actual_costs(self):
        travel_request = make_request(self.owner, RequestState.ACCEPTED, actual='480')
        page = self.client.get(reverse('request_form', args=[travel_request.pk]))
        self.assertContains(page, 'Actual Expenses')
        self.assertContains(page, '480.00 EUR')


class OrganiserMoreTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.organiser = make_user('organiser', [GROUP_REQUESTER])
        self.request = make_request(self.owner, RequestState.SUBMITTED)
        self.request.event.organizers.add(self.organiser)
        self.client.force_login(self.organiser)

    def test_an_organiser_rejects_with_a_reason(self):
        self.client.post(reverse('request_reject', args=[self.request.pk]), {'notes': 'Out of scope'})
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.REJECTED)

    def test_an_organiser_writes_and_reads_internal_notes(self):
        from tsp.models import Comment
        self.client.post(reverse('request_comment', args=[self.request.pk]),
                         {'body': 'Strong talk, worth it', 'internal': 'on'})
        self.assertTrue(Comment.objects.get(body='Strong talk, worth it').internal)
        self.client.force_login(self.owner)
        page = self.client.get(reverse('request_detail', args=[self.request.pk]))
        self.assertNotContains(page, 'Strong talk, worth it')

    def test_an_organiser_downloads_their_events_documents(self):
        from django.core.files.base import ContentFile
        from tsp.models import Document
        doc = Document(request=self.request, kind='receipt', original_name='r.pdf',
                       mimetype='application/pdf', size=4, uploaded_by=self.owner)
        doc.file.save('r.pdf', ContentFile(b'%PDF'), save=True)
        response = self.client.get(reverse('document_download', args=[doc.pk]))
        self.assertEqual(response.status_code, 200)

    def test_an_organiser_does_not_see_drafts(self):
        draft = make_request(self.owner, RequestState.DRAFT)
        draft.event.organizers.add(self.organiser)
        self.assertEqual(self.client.get(reverse('request_detail', args=[draft.pk])).status_code, 403)

    def test_an_organiser_cannot_cancel(self):
        response = self.client.post(reverse('request_cancel', args=[self.request.pk]))
        self.assertEqual(response.status_code, 403)

    def test_bulk_review_only_touches_their_event(self):
        other = make_request(self.owner, RequestState.SUBMITTED)
        other.event.organizers.clear()
        self.client.post(reverse('request_batch_review'),
                         {'request_ids': [self.request.pk, other.pk], 'action': 'approve'})
        self.request.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.APPROVED)
        self.assertEqual(other.state, RequestState.SUBMITTED)

    def test_someone_who_organises_nothing_cannot_bulk_review(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('request_batch_review'),
                                    {'request_ids': [self.request.pk], 'action': 'approve'})
        self.assertEqual(response.status_code, 403)

    def test_organisers_hear_about_new_requests_for_their_event(self):
        from unittest import mock
        draft = make_request(self.owner, RequestState.DRAFT)
        draft.event.organizers.add(self.organiser)
        with mock.patch('profile.services.push_notifications.send_push_to_user') as push:
            from tsp.services.state_machine import transition_request
            transition_request(draft, RequestState.SUBMITTED, self.owner)
        self.assertIn(self.organiser, [c.args[0] for c in push.call_args_list])


class RequestLinkTests(TestCase):

    def test_request_mentions_become_tsp_links(self):
        from tsp.templatetags.tsp_tags import link_requests
        html = link_requests('request 12 <b>')
        self.assertIn(f'href="{reverse("request_detail", args=[12])}"', html)
        self.assertIn('>TSP12</a>', html)
        self.assertIn('&lt;b&gt;', html)
        self.assertIn('>TSP7</a>', link_requests('wise-api TSP7'))


class MultiUploadTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.request = make_request(self.owner, RequestState.ACCEPTED, actual='480')
        self.client.force_login(self.owner)

    def pdf(self, name):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, b'%PDF-1.4 test', content_type='application/pdf')

    def test_three_documents_go_up_together(self):
        self.client.post(reverse('request_document_upload', args=[self.request.pk]), {
            'kind': 'receipt', 'file': self.pdf('r.pdf'),
            'kind_1': 'invoice', 'file_1': self.pdf('i.pdf'),
            'kind_2': 'signed_form', 'file_2': self.pdf('s.pdf'),
        })
        self.assertEqual(self.request.missing_document_kinds(), [])

    def test_one_document_still_works(self):
        self.client.post(reverse('request_document_upload', args=[self.request.pk]),
                         {'kind': 'receipt', 'file': self.pdf('r.pdf')})
        self.assertEqual(self.request.documents.count(), 1)

    def test_one_bad_file_uploads_nothing(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.post(reverse('request_document_upload', args=[self.request.pk]), {
            'kind': 'receipt', 'file': self.pdf('r.pdf'),
            'kind_1': 'invoice', 'file_1': SimpleUploadedFile('x.exe', b'MZ'),
        })
        self.assertEqual(self.request.documents.count(), 0)


class EventMoneyTests(TestCase):

    def test_each_stage_shows_its_requests_and_money(self):
        from tsp.views import _event_money
        owner = make_user('traveller', [GROUP_REQUESTER])
        first = make_request(owner, RequestState.DRAFT, amount='100')
        event = first.event
        for state, amount in ((RequestState.SUBMITTED, '200'), (RequestState.APPROVED, '300'),
                              (RequestState.ACCEPTED, '50'), (RequestState.REJECTED, '999')):
            other = make_request(owner, state, amount=amount)
            other.event = event
            other.save()
        row = next(r for r in _event_money() if r['event'] == event)
        draft, requested, approved, paid = row['stages']
        self.assertEqual((draft['count'], draft['money']), (1, [('EUR', Decimal('100'))]))
        self.assertEqual((requested['count'], requested['money']), (1, [('EUR', Decimal('200'))]))
        self.assertEqual((approved['count'], approved['money']), (2, [('EUR', Decimal('350'))]))
        self.assertEqual(paid['count'], 0)

    def test_finance_sees_the_table(self):
        make_request(make_user('traveller', [GROUP_REQUESTER]), RequestState.SUBMITTED)
        self.client.force_login(make_user('counter', [GROUP_FINANCE]))
        self.assertContains(self.client.get(reverse('dashboard')), 'Money by event')


class WiseBalanceTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.delete('wise-balance')

    def test_finance_sees_the_wise_balance(self):
        from unittest import mock
        wise = mock.Mock()
        wise.list_balances.return_value = [
            {'amount': {'value': 0, 'currency': 'AUD'}},
            {'amount': {'value': 1500.5, 'currency': 'USD'}},
            {'amount': {'value': 926814.02, 'currency': 'EUR'}},
        ]
        self.client.force_login(make_user('counter', [GROUP_FINANCE]))
        with mock.patch('payments.gateways.wise_api.get_wise_client', return_value=wise):
            response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['wise_balance'],
                         [('EUR', Decimal('926814.02')), ('USD', Decimal('1500.5'))])
        self.assertContains(response, 'Wise balance')

    def test_a_wise_outage_does_not_break_the_dashboard(self):
        from unittest import mock
        from payments.gateways.wise_api import WiseApiError
        wise = mock.Mock()
        wise.list_balances.side_effect = WiseApiError('down', 503)
        self.client.force_login(make_user('counter', [GROUP_FINANCE]))
        with mock.patch('payments.gateways.wise_api.get_wise_client', return_value=wise):
            self.assertContains(self.client.get(reverse('dashboard')), 'Wise did not answer')
