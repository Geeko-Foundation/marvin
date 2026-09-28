from datetime import date
from unittest import mock

from django.contrib.auth.models import User
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
        wise_account_id=1, wise_recipient_verified_at=timezone.now(),
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
            wise_account_id=1, wise_recipient_verified_at=timezone.now(),
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
            request=self.request, category='transportation',
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
