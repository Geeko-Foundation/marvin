import os
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP, assign_user_to_group
from profile.models import UserProfile
from tsp.forms import MAX_DOCUMENT_BYTES, DocumentUploadForm
from tsp.models import Comment, Document, Event, Expense, RequestState, StateHistory, TravelRequest
from tsp.services.state_machine import (
    StateTransitionError,
    can_transition,
    transition_request,
)
from tsp.templatetags.tsp_tags import page_url


def make_event():
    return Event.objects.create(
        name='openSUSE Conference',
        location='Nuremberg',
        start_date=date(2027, 6, 14),
        end_date=date(2027, 6, 16),
    )


def make_requester(username):
    """A requester who has finished onboarding, so the middleware lets them through."""
    user = User.objects.create_user(
        username, password='x', email=f'{username}@example.org', first_name='Test',
    )
    profile = UserProfile.objects.create(
        user=user, address_line='1 Main St', city='Pune', country='India',
        postal_code='411001', wise_account_id=1, wise_recipient_verified_at=timezone.now(),
    )
    profile.user.groups.clear()
    assign_user_to_group(user, 'Requester')
    return user


def make_committee(username):
    """Give committee users a profile up front: get_profile() files anyone
    without one under Requester, which would send them into onboarding."""
    user = User.objects.create_user(username, password='x')
    UserProfile.objects.create(user=user)
    assign_user_to_group(user, GROUP_TSP)
    return user


def make_user(username, groups, onboarded=True):
    """A user in the given groups, optionally with onboarding finished."""
    user = User.objects.create_user(
        username, password='x', email=f'{username}@example.org', first_name='Test',
    )
    extra = {}
    if onboarded:
        extra = dict(
            address_line='1 Main St', city='Pune', country='India',
            postal_code='411001', wise_account_id=1,
            wise_recipient_verified_at=timezone.now(),
        )
    UserProfile.objects.create(user=user, **extra)
    for group in groups:
        assign_user_to_group(user, group)
    return user


class StateMachineTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user('req', password='x')
        self.committee = User.objects.create_user('tspuser', password='x')
        self.request = TravelRequest.objects.create(
            user=self.requester, event=make_event(), description='Talk',
        )

    def test_happy_path_reaches_paid(self):
        for state in (
            RequestState.SUBMITTED,
            RequestState.APPROVED,
            RequestState.ACCEPTED,
            RequestState.SUBMITTED_FOR_PAYMENT,
            RequestState.PAID,
        ):
            transition_request(self.request, state, self.committee)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.PAID)
        self.assertEqual(StateHistory.objects.filter(request=self.request).count(), 5)

    def test_cannot_skip_review(self):
        with self.assertRaises(StateTransitionError):
            transition_request(self.request, RequestState.APPROVED, self.committee)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.DRAFT)
        self.assertFalse(StateHistory.objects.filter(request=self.request).exists())

    def test_terminal_states_are_final(self):
        for terminal in (RequestState.REJECTED, RequestState.PAID, RequestState.CANCELLED):
            for target in RequestState.values:
                self.assertFalse(can_transition(terminal, target), f'{terminal} -> {target}')

    def test_approve_and_accept_record_who_and_when(self):
        transition_request(self.request, RequestState.SUBMITTED, self.requester)
        transition_request(self.request, RequestState.APPROVED, self.committee)
        self.assertIsNotNone(self.request.approved_at)
        transition_request(self.request, RequestState.ACCEPTED, self.requester)
        self.assertIsNotNone(self.request.accepted_at)
        self.assertEqual(self.request.accepted_by, self.requester)


class RequestAccessTests(TestCase):
    def setUp(self):
        self.owner = make_requester('owner')
        self.other = make_requester('other')
        self.committee = make_committee('committee')
        self.request = TravelRequest.objects.create(
            user=self.owner, event=make_event(), description='Talk',
        )

    def test_owner_can_view(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 200,
        )

    def test_committee_can_view(self):
        self.client.force_login(self.committee)
        self.assertEqual(
            self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 200,
        )

    def test_other_requester_cannot_view(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 403,
        )

    def test_only_owner_can_submit(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.post(reverse('request_submit', args=[self.request.pk])).status_code, 403,
        )

    def test_requester_cannot_approve(self):
        self.request.state = RequestState.SUBMITTED
        self.request.save()
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(reverse('request_approve', args=[self.request.pk])).status_code, 403,
        )
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED)

    def test_committee_approves_with_amounts(self):
        self.request.state = RequestState.SUBMITTED
        self.request.save()
        expense = Expense.objects.create(
            request=self.request, category='flight',
            description='Flight', estimated_amount=600,
        )
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[self.request.pk]), {
            'expense_id': [str(expense.pk)],
            f'approved_amount_{expense.pk}': '400',
            f'approved_currency_{expense.pk}': 'EUR',
        })
        self.request.refresh_from_db()
        expense.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.APPROVED)
        self.assertEqual(expense.approved_amount, 400)


class DocumentUploadFormTests(TestCase):
    def _form(self, name, content):
        return DocumentUploadForm(
            data={'kind': 'receipt'},
            files={'file': SimpleUploadedFile(name, content)},
        )

    def test_accepts_pdfs_and_photos(self):
        for name, content in (
            ('receipt.pdf', b'%PDF-1.7 receipt'),
            ('photo.png', b'\x89PNG\r\n\x1a\n data'),
            ('photo.JPG', b'\xff\xd8\xff\xe0 data'),
        ):
            self.assertTrue(self._form(name, content).is_valid(), name)

    def test_rejects_other_file_types(self):
        self.assertFalse(self._form('notes.docx', b'PK\x03\x04').is_valid())

    def test_rejects_a_script_renamed_as_pdf(self):
        self.assertFalse(self._form('receipt.pdf', b'#!/bin/sh\necho hello').is_valid())

    def test_rejects_files_over_the_limit(self):
        oversized = b'%PDF-' + b'0' * MAX_DOCUMENT_BYTES
        self.assertFalse(self._form('big.pdf', oversized).is_valid())

    def test_rejected_upload_is_not_saved(self):
        owner = make_requester('uploader')
        travel_request = TravelRequest.objects.create(
            user=owner, event=make_event(), description='Talk',
        )
        self.client.force_login(owner)
        self.client.post(
            reverse('request_document_upload', args=[travel_request.pk]),
            {'kind': 'receipt', 'file': SimpleUploadedFile('receipt.pdf', b'#!/bin/sh\necho hello')},
        )
        self.assertEqual(Document.objects.count(), 0)


class PaginationTests(TestCase):
    def test_requester_dashboard_pages_through_requests(self):
        user = make_requester('pager')
        event = make_event()
        for i in range(25):
            TravelRequest.objects.create(user=user, event=event, description=f'request {i}')

        self.client.force_login(user)
        first = self.client.get(reverse('dashboard'))
        self.assertEqual(len(first.context['my_requests']), 20)

        second = self.client.get(reverse('dashboard'), {'page': 2})
        self.assertEqual(len(second.context['my_requests']), 5)
        self.assertContains(second, 'join-item')
        self.assertContains(second, 'Page 2 of 2')

    def test_committee_stat_counts_every_pending_request(self):
        owner = make_requester('statowner')
        event = make_event()
        for i in range(25):
            TravelRequest.objects.create(
                user=owner, event=event, description=f'request {i}',
                state=RequestState.SUBMITTED,
            )

        self.client.force_login(make_committee('statcommittee'))
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(len(response.context['pending']), 20)
        self.assertContains(response, '>25<')

    def test_csv_export_is_not_limited_to_one_page(self):
        owner = make_requester('csvowner')
        event = make_event()
        for i in range(105):
            TravelRequest.objects.create(user=owner, event=event, description=f'request {i}')

        self.client.force_login(make_committee('csvcommittee'))
        response = self.client.get(reverse('reports'), {'format': 'csv'})
        rows = response.content.decode().splitlines()
        self.assertEqual(len(rows), 106)  # header + every matching request

    def test_page_links_keep_the_current_filters(self):
        request = RequestFactory().get('/reports/', {'status': 'paid', 'page': '1'})
        url = page_url({'request': request}, 'page', 2)
        self.assertIn('status=paid', url)
        self.assertIn('page=2', url)
        self.assertNotIn('page=1', url)


class MultiRoleDashboardTests(TestCase):
    """A user in several groups should see every section they are entitled to."""

    def dashboard_for(self, username, groups, **kwargs):
        self.client.force_login(make_user(username, groups, **kwargs))
        return self.client.get(reverse('dashboard'))

    def test_requester_and_committee_see_both_sections(self):
        response = self.dashboard_for('both', [GROUP_REQUESTER, GROUP_TSP])
        self.assertContains(response, 'My Requests')
        self.assertContains(response, 'Pending Review Queue')

    def test_committee_and_finance_see_both_sections(self):
        response = self.dashboard_for('cf', [GROUP_TSP, GROUP_FINANCE])
        self.assertContains(response, 'Pending Review Queue')
        self.assertContains(response, 'Payment Queue')

    def test_all_three_roles_see_everything(self):
        response = self.dashboard_for('all', [GROUP_REQUESTER, GROUP_TSP, GROUP_FINANCE])
        self.assertContains(response, 'My Requests')
        self.assertContains(response, 'Pending Review Queue')
        self.assertContains(response, 'Payment Queue')

    def test_single_role_sees_only_its_own_section(self):
        response = self.dashboard_for('justme', [GROUP_REQUESTER])
        self.assertContains(response, 'My Requests')
        self.assertNotContains(response, 'Pending Review Queue')
        self.assertNotContains(response, 'Payment Queue')

    def test_user_with_no_group_is_refused(self):
        self.client.force_login(make_user('nobody', []))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 403)


class OnboardingGateTests(TestCase):
    def test_requester_only_is_still_sent_through_onboarding(self):
        self.client.force_login(make_user('newreq', [GROUP_REQUESTER], onboarded=False))
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('signup', response.url)

    def test_committee_member_is_not_trapped_in_onboarding(self):
        self.client.force_login(make_user('newcom', [GROUP_REQUESTER, GROUP_TSP], onboarded=False))
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pending Review Queue')

    def test_but_they_still_cannot_create_a_request_without_a_payout_account(self):
        self.client.force_login(make_user('nopayout', [GROUP_REQUESTER, GROUP_TSP], onboarded=False))
        response = self.client.get(reverse('request_create'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('signup', response.url)

    def test_and_cannot_submit_one_either(self):
        user = make_user('nopayout2', [GROUP_REQUESTER, GROUP_TSP], onboarded=False)
        travel_request = TravelRequest.objects.create(
            user=user, event=make_event(), description='Talk',
        )
        self.client.force_login(user)
        response = self.client.post(reverse('request_submit', args=[travel_request.pk]))
        self.assertEqual(response.status_code, 302)
        travel_request.refresh_from_db()
        self.assertEqual(travel_request.state, RequestState.DRAFT)


class EventManagementTests(TestCase):
    def setUp(self):
        self.committee = make_committee('eventcommittee')
        self.requester = make_requester('eventrequester')
        self.event = make_event()

    def test_committee_can_edit_an_event(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('event_edit', args=[self.event.pk]), {
            'name': 'Renamed', 'location': 'Prague',
            'start_date': '2027-06-14', 'end_date': '2027-06-16',
        })
        self.event.refresh_from_db()
        self.assertEqual(self.event.name, 'Renamed')
        self.assertFalse(self.event.accepting_requests)

    def test_requester_cannot_edit_an_event(self):
        self.client.force_login(self.requester)
        response = self.client.get(reverse('event_edit', args=[self.event.pk]))
        self.assertEqual(response.status_code, 403)

    def test_committee_can_delete_an_event_nobody_has_applied_to(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('event_delete', args=[self.event.pk]))
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())

    def test_an_event_with_requests_is_not_deleted(self):
        travel_request = TravelRequest.objects.create(
            user=self.requester, event=self.event, description='Talk',
        )
        self.client.force_login(self.committee)
        self.client.post(reverse('event_delete', args=[self.event.pk]))
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(TravelRequest.objects.filter(pk=travel_request.pk).exists())

    def test_requester_cannot_delete_an_event(self):
        self.client.force_login(self.requester)
        response = self.client.post(reverse('event_delete', args=[self.event.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_delete_needs_a_post(self):
        self.client.force_login(self.committee)
        response = self.client.get(reverse('event_delete', args=[self.event.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_delete_button_only_shows_for_empty_events(self):
        busy = make_event()
        TravelRequest.objects.create(user=self.requester, event=busy, description='Talk')
        self.client.force_login(self.committee)
        response = self.client.get(reverse('event_list'))
        self.assertContains(response, reverse('event_delete', args=[self.event.pk]))
        self.assertNotContains(response, reverse('event_delete', args=[busy.pk]))
class InternalNotesTests(TestCase):
    PUBLIC = 'Looks fine to me'
    INTERNAL = 'Hotel estimate is well above our usual cap'

    def setUp(self):
        self.owner = make_requester('noteowner')
        self.committee = make_committee('notecommittee')
        self.finance = make_user('notefinance', [GROUP_FINANCE])
        self.request = TravelRequest.objects.create(
            user=self.owner, event=make_event(), description='Talk',
        )
        Comment.objects.create(request=self.request, user=self.committee, body=self.PUBLIC)
        Comment.objects.create(request=self.request, user=self.committee, body=self.INTERNAL, internal=True)

    def detail_as(self, user, travel_request=None):
        self.client.force_login(user)
        return self.client.get(reverse('request_detail', args=[(travel_request or self.request).pk]))

    def test_committee_sees_internal_notes(self):
        response = self.detail_as(self.committee)
        self.assertContains(response, self.INTERNAL)
        self.assertContains(response, self.PUBLIC)

    def test_finance_sees_internal_notes(self):
        self.assertContains(self.detail_as(self.finance), self.INTERNAL)

    def test_requester_does_not_see_internal_notes(self):
        response = self.detail_as(self.owner)
        self.assertContains(response, self.PUBLIC)
        self.assertNotContains(response, self.INTERNAL)
        self.assertNotContains(response, 'Internal note, visible only')

    def test_committee_member_does_not_see_notes_on_their_own_request(self):
        member = make_user('ownsone', [GROUP_REQUESTER, GROUP_TSP])
        own = TravelRequest.objects.create(user=member, event=make_event(), description='Mine')
        Comment.objects.create(request=own, user=self.committee, body=self.INTERNAL, internal=True)
        self.assertNotContains(self.detail_as(member, own), self.INTERNAL)

    def test_requester_cannot_post_an_internal_note(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('request_comment', args=[self.request.pk]),
                         {'body': 'sneaky', 'internal': 'on'})
        self.assertFalse(Comment.objects.get(body='sneaky').internal)

    def test_committee_can_post_an_internal_note(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('request_comment', args=[self.request.pk]),
                         {'body': 'just us', 'internal': 'on'})
        self.assertTrue(Comment.objects.get(body='just us').internal)

    @mock.patch('profile.services.push_notifications.send_push_to_user')
    def test_internal_note_never_notifies_the_requester(self, send):
        self.client.force_login(self.committee)
        self.client.post(reverse('request_comment', args=[self.request.pk]),
                         {'body': 'just us', 'internal': 'on'})
        notified = [call.args[0] for call in send.call_args_list]
        self.assertNotIn(self.owner, notified)

    @mock.patch('profile.services.push_notifications.send_push_to_user')
    def test_ordinary_comment_still_notifies_the_requester(self, send):
        self.client.force_login(self.committee)
        self.client.post(reverse('request_comment', args=[self.request.pk]), {'body': 'hello'})
        notified = [call.args[0] for call in send.call_args_list]
        self.assertIn(self.owner, notified)


class ActualCostTests(TestCase):
    """Reimbursement is the lesser of what was approved and what was spent."""

    def setUp(self):
        self.owner = make_user('actualowner', [GROUP_REQUESTER])
        self.event = Event.objects.create(
            name='Conf', location='Berlin',
            start_date=date(2099, 5, 1), end_date=date(2099, 5, 3),
        )
        self.request = TravelRequest.objects.create(
            user=self.owner, event=self.event, state=RequestState.ACCEPTED,
            description='Trip',
        )

    def _expense(self, approved, actual=None, currency='EUR', actual_currency=None):
        return Expense.objects.create(
            request=self.request, category='flight', description='Flight',
            estimated_amount=approved, estimated_currency=currency,
            approved_amount=approved, approved_currency=currency,
            actual_amount=actual,
            actual_currency=actual_currency or (currency if actual is not None else None),
        )

    def test_spending_less_than_approved_pays_the_lower_figure(self):
        expense = self._expense(600, 400)
        self.assertEqual(expense.payable_amount(), 400)
        self.assertEqual(self.request.total_payable(), 400)

    def test_spending_more_than_approved_still_pays_the_approved_figure(self):
        expense = self._expense(600, 800)
        self.assertEqual(expense.payable_amount(), 600)
        self.assertEqual(self.request.total_payable(), 600)

    def test_the_lesser_is_taken_line_by_line(self):
        # Approved 300 + 300, spent 400 + 100. Per line that is 300 + 100 = 400.
        # Comparing the totals instead would pay 500, letting an overspend on one
        # line be covered by an underspend on another.
        self._expense(300, 400)
        self._expense(300, 100)
        self.assertEqual(self.request.total_approved(), 600)
        self.assertEqual(self.request.total_actual(), 500)
        self.assertEqual(self.request.total_payable(), 400)

    def test_nothing_reported_yet_falls_back_to_approved(self):
        expense = self._expense(600)
        self.assertEqual(expense.payable_amount(), 600)
        self.assertFalse(self.request.has_all_actual_amounts())

    def test_a_receipt_in_another_currency_is_left_for_finance(self):
        expense = self._expense(600, 400, currency='EUR', actual_currency='USD')
        self.assertTrue(expense.has_currency_mismatch())
        # 400 USD is not necessarily less than 600 EUR, so it is not guessed at.
        self.assertEqual(expense.payable_amount(), 600)
        self.assertEqual(self.request.expenses_needing_a_decision(), [expense])

    def test_finance_is_asked_to_pay_the_payable_total(self):
        from tsp.payables import TravelRequestPayableAdapter

        self._expense(600, 400)
        self.request.state = RequestState.SUBMITTED_FOR_PAYMENT
        self.request.save()
        payment = TravelRequestPayableAdapter().build_payment_request(self.request)
        self.assertEqual(payment.amount, Decimal('400'))

    def test_a_request_cannot_go_to_finance_without_actual_costs(self):
        self._expense(600)
        for kind in ('receipt', 'invoice', 'signed_form'):
            Document.objects.create(
                request=self.request, kind=kind, size=9, uploaded_by=self.owner,
                file=SimpleUploadedFile(f'{kind}.pdf', b'%PDF-1.4 x'),
            )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('request_submit_for_payment', args=[self.request.pk]),
        )
        self.assertRedirects(response, reverse('request_actuals', args=[self.request.pk]))
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.ACCEPTED)

    def test_the_requestor_reports_actual_costs(self):
        expense = self._expense(600)
        self.client.force_login(self.owner)
        response = self.client.post(reverse('request_actuals', args=[self.request.pk]), {
            'expenses-TOTAL_FORMS': '1',
            'expenses-INITIAL_FORMS': '1',
            'expenses-MIN_NUM_FORMS': '0',
            'expenses-MAX_NUM_FORMS': '1000',
            'expenses-0-id': str(expense.pk),
            'expenses-0-actual_amount': '425.50',
            'expenses-0-actual_currency': 'EUR',
        })
        self.assertRedirects(response, reverse('request_detail', args=[self.request.pk]))
        expense.refresh_from_db()
        self.assertEqual(expense.actual_amount, Decimal('425.50'))
        self.assertEqual(self.request.total_payable(), Decimal('425.50'))

    def test_only_the_owner_can_report_them(self):
        self._expense(600)
        self.client.force_login(make_user('someoneelse', [GROUP_REQUESTER]))
        response = self.client.get(reverse('request_actuals', args=[self.request.pk]))
        self.assertEqual(response.status_code, 403)


class SelfNotificationTests(TestCase):
    """Nobody should be told about something they just did themselves."""

    def setUp(self):
        from django.core import mail

        self.mail = mail
        self.owner = make_user('selfowner', [GROUP_REQUESTER])
        self.chair = make_user('selfchair', [GROUP_TSP])
        self.event = Event.objects.create(
            name='Conf', location='Berlin',
            start_date=date(2027, 5, 1), end_date=date(2027, 5, 3))
        self.request = TravelRequest.objects.create(
            user=self.owner, event=self.event,
            state=RequestState.APPROVED, description='T')

    def _recipients(self):
        return [to for m in self.mail.outbox for to in m.to]

    def test_accepting_does_not_email_the_person_who_accepted(self):
        from tsp.services.state_machine import transition_request

        transition_request(self.request, RequestState.ACCEPTED, self.owner)
        self.assertNotIn(self.owner.email, self._recipients())

    def test_accepting_tells_the_committee_instead(self):
        from tsp.services.state_machine import transition_request

        transition_request(self.request, RequestState.ACCEPTED, self.owner)
        self.assertIn(self.chair.email, self._recipients())

    def test_the_committee_member_who_acts_is_not_told_either(self):
        from tsp.services.state_machine import transition_request

        self.request.state = RequestState.SUBMITTED
        self.request.save()
        transition_request(self.request, RequestState.APPROVED, self.chair)
        self.assertNotIn(self.chair.email, self._recipients())
        self.assertIn(self.owner.email, self._recipients())


class DocumentRemovalTests(TestCase):
    """A document uploaded by mistake has to be removable, up to a point."""

    def setUp(self):
        self.owner = make_user('docowner', [GROUP_REQUESTER])
        self.event = Event.objects.create(
            name='Conf', location='Berlin',
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 3))
        self.request = TravelRequest.objects.create(
            user=self.owner, event=self.event, state=RequestState.ACCEPTED,
            description='Trip')
        self.doc = self._doc()

    def _doc(self, kind='invoice'):
        return Document.objects.create(
            request=self.request, kind=kind, size=9, uploaded_by=self.owner,
            file=SimpleUploadedFile(f'{kind}.pdf', b'%PDF-1.4 x'))

    def _delete(self, doc=None):
        return self.client.post(reverse('request_document_delete',
                                        args=[self.request.pk, (doc or self.doc).pk]))

    def test_the_requester_can_remove_a_duplicate(self):
        extra = self._doc()
        self.assertEqual(self.request.documents.count(), 2)
        self.client.force_login(self.owner)
        self._delete(extra)
        self.assertEqual(self.request.documents.count(), 1)

    def test_the_file_is_removed_from_storage_too(self):
        path = self.doc.file.path
        self.client.force_login(self.owner)
        self._delete()
        self.assertFalse(os.path.exists(path))

    def test_nobody_else_can_remove_it(self):
        self.client.force_login(make_user('otherperson', [GROUP_REQUESTER]))
        self.assertEqual(self._delete().status_code, 403)
        self.assertEqual(self.request.documents.count(), 1)

    def test_the_committee_cannot_remove_someone_else_s_evidence(self):
        self.client.force_login(make_user('nosycommittee', [GROUP_TSP]))
        self.assertEqual(self._delete().status_code, 403)
        self.assertEqual(self.request.documents.count(), 1)

    def test_documents_are_kept_once_the_request_is_with_finance(self):
        for state in (RequestState.SUBMITTED_FOR_PAYMENT, RequestState.PAID):
            with self.subTest(state=state):
                self.request.state = state
                self.request.save()
                self.client.force_login(self.owner)
                self._delete()
                self.assertEqual(self.request.documents.count(), 1)


class EventBudgetTests(TestCase):
    """An event's budget is what the committee has to spend on it."""

    def setUp(self):
        self.committee = make_user('budgetcommittee', [GROUP_TSP])
        self.owner = make_user('budgetowner', [GROUP_REQUESTER])
        self.event = Event.objects.create(
            name='Summit', location='Berlin', budget=Decimal('800.00'),
            budget_currency='EUR',
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 3))

    def _request(self, amount, currency='EUR', state=RequestState.APPROVED, user=None):
        travel_request = TravelRequest.objects.create(
            user=user or self.owner, event=self.event, state=state, description='T')
        Expense.objects.create(
            request=travel_request, category='flight', description='Flight',
            estimated_amount=amount, estimated_currency=currency,
            approved_amount=amount, approved_currency=currency)
        return travel_request

    def test_remaining_budget_counts_what_is_approved(self):
        self._request(Decimal('700.00'))
        self.assertEqual(self.event.approved_against_budget(), Decimal('700.00'))
        self.assertEqual(self.event.budget_remaining(), Decimal('100.00'))

    def test_rejected_and_cancelled_requests_free_their_budget(self):
        self._request(Decimal('700.00'), state=RequestState.REJECTED)
        self._request(Decimal('100.00'), state=RequestState.CANCELLED)
        self.assertEqual(self.event.budget_remaining(), Decimal('800.00'))

    def test_an_event_without_a_budget_has_no_limit(self):
        self.event.budget = None
        self.event.save()
        self._request(Decimal('5000.00'))
        self.assertIsNone(self.event.budget_remaining())

    def test_other_currencies_are_reported_not_converted(self):
        self._request(Decimal('100.00'), currency='USD')
        self.assertEqual(self.event.approved_against_budget(), Decimal('0'))
        self.assertEqual(self.event.approved_in_other_currencies(),
                         [('USD', Decimal('100.00'))])
        self.assertEqual(self.event.budget_remaining(), Decimal('800.00'))

    def test_approving_past_the_budget_is_refused(self):
        self._request(Decimal('800.00'))
        pending = TravelRequest.objects.create(
            user=make_user('second', [GROUP_REQUESTER]), event=self.event,
            state=RequestState.SUBMITTED, description='Second')
        expense = Expense.objects.create(
            request=pending, category='lodging', description='Hotel',
            estimated_amount=Decimal('110.00'), estimated_currency='EUR')
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[pending.pk]), {
            'expense_id': [str(expense.pk)],
            f'approved_amount_{expense.pk}': '110.00',
            f'approved_currency_{expense.pk}': 'EUR',
        })
        pending.refresh_from_db()
        self.assertEqual(pending.state, RequestState.SUBMITTED)

    def test_the_committee_can_approve_anyway_and_it_is_recorded(self):
        self._request(Decimal('800.00'))
        pending = TravelRequest.objects.create(
            user=make_user('third', [GROUP_REQUESTER]), event=self.event,
            state=RequestState.SUBMITTED, description='Third')
        expense = Expense.objects.create(
            request=pending, category='lodging', description='Hotel',
            estimated_amount=Decimal('110.00'), estimated_currency='EUR')
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[pending.pk]), {
            'expense_id': [str(expense.pk)],
            f'approved_amount_{expense.pk}': '110.00',
            f'approved_currency_{expense.pk}': 'EUR',
            'over_budget': '1',
        })
        pending.refresh_from_db()
        self.assertEqual(pending.state, RequestState.APPROVED)
        latest = pending.state_history.order_by('-id').first()
        self.assertIn('over budget', latest.notes)

    def test_approving_within_budget_is_untouched(self):
        pending = TravelRequest.objects.create(
            user=self.owner, event=self.event,
            state=RequestState.SUBMITTED, description='Within')
        expense = Expense.objects.create(
            request=pending, category='lodging', description='Hotel',
            estimated_amount=Decimal('100.00'), estimated_currency='EUR')
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[pending.pk]), {
            'expense_id': [str(expense.pk)],
            f'approved_amount_{expense.pk}': '100.00',
            f'approved_currency_{expense.pk}': 'EUR',
        })
        pending.refresh_from_db()
        self.assertEqual(pending.state, RequestState.APPROVED)


class ReportFilterTests(TestCase):
    """Reports are the committee's way of finding a request after the fact."""

    def setUp(self):
        self.staff = make_user('reportstaff', [GROUP_FINANCE])
        self.alice = make_user('alice', [GROUP_REQUESTER])
        self.bob = make_user('bob', [GROUP_REQUESTER])
        self.conf = Event.objects.create(
            name='Summit', location='Berlin',
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 3))
        self.other = Event.objects.create(
            name='Workshop', location='Pune',
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 2))
        TravelRequest.objects.create(user=self.alice, event=self.conf,
                                     state=RequestState.SUBMITTED, description='A')
        TravelRequest.objects.create(user=self.bob, event=self.other,
                                     state=RequestState.APPROVED, description='B')
        self.client.force_login(self.staff)

    def _rows(self, **params):
        response = self.client.get(reverse('reports'), params)
        return [r.user.username for r in response.context['requests_list']]

    def test_every_state_can_be_filtered_for(self):
        from tsp.forms import ReportFilterForm

        offered = {value for value, _ in ReportFilterForm().fields['status'].choices if value}
        # Draft, submitted and approved used to be missing from the dropdown.
        self.assertEqual(offered, {state.value for state in RequestState})

    def test_filtering_by_a_previously_missing_state_works(self):
        self.assertEqual(self._rows(status=RequestState.APPROVED), ['bob'])
        self.assertEqual(self._rows(status=RequestState.SUBMITTED), ['alice'])

    def test_searching_by_requester(self):
        self.assertEqual(self._rows(q='alice'), ['alice'])

    def test_searching_by_event(self):
        self.assertEqual(self._rows(q='Workshop'), ['bob'])

    def test_an_empty_search_returns_everything(self):
        self.assertEqual(sorted(self._rows(q='')), ['alice', 'bob'])


class EventDeadlineTests(TestCase):
    """Events close themselves, so nobody has to remember to untick a box."""

    def _event(self, **kwargs):
        defaults = dict(
            name='Summit', location='Berlin',
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 3))
        return Event.objects.create(**{**defaults, **kwargs})

    def test_requests_close_on_their_date_without_anyone_unticking(self):
        past = self._event(requests_open_until=date(2026, 1, 1))
        future = self._event(end_date=date(2099, 5, 3),
                             requests_open_until=date(2099, 1, 1))
        self.assertFalse(past.is_open_for_requests())
        self.assertTrue(future.is_open_for_requests())

    def test_the_checkbox_still_closes_an_event_on_its_own(self):
        event = self._event(end_date=date(2099, 5, 3),
                            requests_open_until=date(2099, 1, 1),
                            accepting_requests=False)
        self.assertFalse(event.is_open_for_requests())

    def test_an_event_that_has_already_happened_takes_no_more_requests(self):
        self.assertFalse(self._event().is_open_for_requests())

    def test_an_event_with_no_date_follows_the_checkbox(self):
        self.assertTrue(self._event(end_date=date(2099, 5, 3)).is_open_for_requests())

    def test_claims_default_to_the_organisation_window(self):
        event = self._event(end_date=date(2026, 5, 3))
        # settings.REIMBURSEMENT_WINDOW_DAYS is 30 unless an org changes it.
        self.assertEqual(event.reimbursement_deadline(), date(2026, 6, 2))

    def test_an_event_can_set_its_own_claim_deadline(self):
        event = self._event(reimbursements_open_until=date(2026, 9, 1))
        self.assertEqual(event.reimbursement_deadline(), date(2026, 9, 1))

    def test_a_closed_window_is_reported_as_closed(self):
        self.assertTrue(self._event(end_date=date(2020, 1, 1)).reimbursement_window_closed())
        self.assertFalse(self._event(end_date=date(2099, 1, 1)).reimbursement_window_closed())

    def test_a_late_claim_is_refused(self):
        owner = make_user('lateclaimer', [GROUP_REQUESTER])
        event = self._event(end_date=date(2020, 1, 1))
        req = TravelRequest.objects.create(
            user=owner, event=event, state=RequestState.ACCEPTED, description='T')
        Expense.objects.create(
            request=req, category='flight', description='Flight',
            estimated_amount=100, approved_amount=100, approved_currency='EUR',
            actual_amount=90, actual_currency='EUR')
        for kind in ('receipt', 'invoice', 'signed_form'):
            Document.objects.create(request=req, kind=kind, size=9, uploaded_by=owner,
                                    file=SimpleUploadedFile(f'{kind}.pdf', b'%PDF-1.4 x'))
        self.client.force_login(owner)
        self.client.post(reverse('request_submit_for_payment', args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.state, RequestState.ACCEPTED)


class ClaimReminderTests(TestCase):
    """People forget to claim, so the deadline needs to chase them."""

    def setUp(self):
        self.owner = make_user('reminderme', [GROUP_REQUESTER])
        self.owner.email = 'reminderme@example.org'
        self.owner.save()

    def _request_closing_in(self, days, state=RequestState.ACCEPTED):
        deadline = timezone.now().date() + timedelta(days=days)
        event = Event.objects.create(
            name=f'Event closing in {days}', location='Berlin',
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 3),
            reimbursements_open_until=deadline)
        return TravelRequest.objects.create(
            user=self.owner, event=event, state=state, description='T')

    def _run(self, **kwargs):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command('send_claim_reminders', stdout=out, **kwargs)
        return out.getvalue()

    def test_a_reminder_goes_out_at_the_set_intervals(self):
        self._request_closing_in(7)
        output = self._run()
        self.assertIn('reminderme@example.org', output)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('7 day(s) left', mail.outbox[0].subject)

    def test_nothing_is_sent_on_an_ordinary_day(self):
        self._request_closing_in(9)
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_a_dry_run_sends_nothing(self):
        self._request_closing_in(7)
        output = self._run(dry_run=True)
        self.assertIn('Would remind 1', output)
        self.assertEqual(len(mail.outbox), 0)

    def test_requests_that_already_moved_on_are_left_alone(self):
        self._request_closing_in(7, state=RequestState.PAID)
        self._request_closing_in(7, state=RequestState.SUBMITTED_FOR_PAYMENT)
        self._run()
        self.assertEqual(len(mail.outbox), 0)


class EventSpendTests(TestCase):
    """What was approved and what actually left the account are not the same."""

    def setUp(self):
        self.owner = make_user('spender', [GROUP_REQUESTER])
        self.event = Event.objects.create(
            name='Summit', location='Berlin', budget=Decimal('800.00'),
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 3))

    def _paid_request(self, approved, paid=None, currency='EUR'):
        from django.contrib.contenttypes.models import ContentType
        from payments.models import PaymentRecord

        req = TravelRequest.objects.create(
            user=self.owner, event=self.event, state=RequestState.PAID, description='T')
        Expense.objects.create(
            request=req, category='flight', description='Flight',
            estimated_amount=approved, estimated_currency=currency,
            approved_amount=approved, approved_currency=currency)
        if paid is not None:
            PaymentRecord.objects.create(
                content_type=ContentType.objects.get_for_model(TravelRequest),
                object_id=req.pk, gateway_id='manual', amount=paid, currency=currency,
                recipient_name='X', recipient_email='x@example.org',
                reference=f'TSP{req.pk}', status='completed')
        return req

    def test_paid_out_is_read_from_the_payments(self):
        self._paid_request(Decimal('300.00'), paid=Decimal('250.00'))
        self.assertEqual(self.event.paid_by_currency(), [('EUR', Decimal('250.00'))])
        self.assertEqual(self.event.approved_against_budget(), Decimal('300.00'))

    def test_a_request_marked_paid_with_no_payment_counts_as_nothing_spent(self):
        # Flipping a state in the admin must not look like money going out.
        self._paid_request(Decimal('300.00'), paid=None)
        self.assertEqual(self.event.paid_by_currency(), [])

    def test_each_currency_is_reported_separately(self):
        self._paid_request(Decimal('300.00'), paid=Decimal('300.00'), currency='EUR')
        self._paid_request(Decimal('100.00'), paid=Decimal('100.00'), currency='USD')
        self.assertEqual(
            self.event.paid_by_currency(),
            [('EUR', Decimal('300.00')), ('USD', Decimal('100.00'))])


class AuditLogTests(TestCase):
    """Patrick asked for every action logged, and the log not alterable."""

    def setUp(self):
        from profile.audit_log import AuditLogEntry

        self.entries = AuditLogEntry
        self.owner = make_user('audited', [GROUP_REQUESTER])
        self.committee = make_user('auditcommittee', [GROUP_TSP])
        self.event = Event.objects.create(
            name='Summit', location='Berlin',
            start_date=date(2099, 5, 1), end_date=date(2099, 5, 3))
        self.request = TravelRequest.objects.create(
            user=self.owner, event=self.event, state=RequestState.SUBMITTED,
            description='Trip')

    def test_a_state_change_is_recorded(self):
        from tsp.services.state_machine import transition_request

        transition_request(self.request, RequestState.APPROVED, self.committee, 'looks fine')
        entry = self.entries.objects.filter(action='request.state_change').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.user, self.committee)
        self.assertIn('submitted -> approved', entry.detail)
        self.assertIn('looks fine', entry.detail)

    def test_an_entry_cannot_be_changed_once_written(self):
        from tsp.services.state_machine import transition_request

        transition_request(self.request, RequestState.APPROVED, self.committee)
        entry = self.entries.objects.first()
        entry.detail = 'something else'
        with self.assertRaises(ValueError):
            entry.save()

    def test_an_entry_cannot_be_deleted(self):
        from tsp.services.state_machine import transition_request

        transition_request(self.request, RequestState.APPROVED, self.committee)
        with self.assertRaises(ValueError):
            self.entries.objects.first().delete()

    def test_removing_a_document_is_recorded(self):
        self.request.state = RequestState.ACCEPTED
        self.request.save()
        doc = Document.objects.create(
            request=self.request, kind='receipt', size=9, uploaded_by=self.owner,
            file=SimpleUploadedFile('receipt.pdf', b'%PDF-1.4 x'))
        self.client.force_login(self.owner)
        self.client.post(reverse('request_document_delete', args=[self.request.pk, doc.pk]))
        actions = set(self.entries.objects.values_list('action', flat=True))
        self.assertIn('document.removed', actions)

    def test_logging_never_breaks_the_action_it_records(self):
        from unittest import mock
        from tsp.services.state_machine import transition_request

        with mock.patch('profile.audit_log.AuditLogEntry.objects.create',
                        side_effect=RuntimeError('database on fire')):
            transition_request(self.request, RequestState.APPROVED, self.committee)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.APPROVED)


class EventAuditTests(TestCase):
    """Events are the committee's, so creating and changing them is logged."""

    def setUp(self):
        from profile.audit_log import AuditLogEntry

        self.entries = AuditLogEntry
        self.committee = make_user('eventauditor', [GROUP_TSP])
        self.client.force_login(self.committee)

    def _actions(self):
        return list(self.entries.objects.values_list('action', flat=True))

    def test_creating_an_event_is_recorded(self):
        self.client.post(reverse('event_create'), {
            'name': 'Audited Summit', 'location': 'Berlin',
            'start_date': '2099-05-01', 'end_date': '2099-05-03',
            'budget': '500.00', 'budget_currency': 'EUR'})
        entry = self.entries.objects.filter(action='event.created').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.user, self.committee)
        self.assertEqual(entry.target, 'Audited Summit')
        self.assertIn('budget 500.00 EUR', entry.detail)

    def test_editing_an_event_records_what_changed(self):
        event = Event.objects.create(
            name='Summit', location='Berlin',
            start_date=date(2099, 5, 1), end_date=date(2099, 5, 3))
        self.client.post(reverse('event_edit', args=[event.pk]), {
            'name': 'Renamed Summit', 'location': 'Berlin',
            'start_date': '2099-05-01', 'end_date': '2099-05-03'})
        entry = self.entries.objects.filter(action='event.updated').first()
        self.assertIsNotNone(entry)
        self.assertIn('name', entry.detail)

    def test_an_ordinary_upload_is_not_logged(self):
        # Routine requester activity would bury the decisions that matter.
        owner = make_user('uploader', [GROUP_REQUESTER])
        event = Event.objects.create(
            name='E', location='B',
            start_date=date(2099, 5, 1), end_date=date(2099, 5, 3))
        req = TravelRequest.objects.create(
            user=owner, event=event, state=RequestState.ACCEPTED, description='T')
        self.client.force_login(owner)
        self.client.post(reverse('request_document_upload', args=[req.pk]), {
            'kind': 'receipt',
            'file': SimpleUploadedFile('r.pdf', b'%PDF-1.4 x'),
        })
        self.assertNotIn('document.uploaded', self._actions())
