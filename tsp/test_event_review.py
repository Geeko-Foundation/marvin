from datetime import date

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from tsp.forms import EventForm, TravelRequestForm
from tsp.models import Event
from tsp.tests import make_committee, make_event, make_requester


class OrganiserTests(TestCase):
    """Organisers review their event's requests, so they cannot apply to it, and edit only its details."""

    def setUp(self):
        self.event = make_event()
        self.organiser = make_requester('evorganiser')
        self.event.organizers.add(self.organiser)
        self.client.force_login(self.organiser)

    def test_their_own_event_is_not_offered_to_apply_to(self):
        other = make_event()
        events = TravelRequestForm(user=self.organiser).fields['event'].queryset
        self.assertNotIn(self.event, events)
        self.assertIn(other, events)
        page = self.client.get(reverse('event_list'))
        self.assertNotContains(page, f'?event={self.event.pk}')
        self.assertContains(page, f'?event={other.pk}')

    def test_they_edit_the_details_but_not_the_dates_or_budget(self):
        page = self.client.get(reverse('event_edit', args=[self.event.pk]))
        self.assertContains(page, 'Visa letters offered')
        self.assertNotContains(page, 'name="start_date"')
        self.client.post(reverse('event_edit', args=[self.event.pk]), {
            'location': 'Prague', 'url': '', 'description': 'Updated', 'start_date': '2030-01-01', 'budget': '1',
        })
        self.event.refresh_from_db()
        self.assertEqual((self.event.location, self.event.description), ('Prague', 'Updated'))
        self.assertEqual(self.event.start_date, date(2027, 6, 14))
        self.assertIsNone(self.event.budget)

    def test_someone_else_cannot_edit_it(self):
        self.client.force_login(make_requester('evstranger'))
        self.assertEqual(self.client.get(reverse('event_edit', args=[self.event.pk])).status_code, 403)


class ProposalReviewTests(TestCase):
    """The committee accepts or turns down a proposed event instead of it just sitting closed."""

    def setUp(self):
        self.proposer = make_requester('evproposer')
        self.committee = make_committee('evcommittee')
        self.event = Event.objects.create(
            name='Asia Summit', location='India', start_date=date(2027, 12, 20), end_date=date(2027, 12, 23),
            accepting_requests=False, proposed_by=self.proposer,
        )

    def test_the_list_shows_it_as_proposed_with_both_choices(self):
        self.client.force_login(self.committee)
        page = self.client.get(reverse('event_list'))
        self.assertContains(page, 'Proposed')
        self.assertContains(page, reverse('event_accept', args=[self.event.pk]))
        self.assertContains(page, reverse('event_reject', args=[self.event.pk]))

    def test_accepting_opens_it_and_tells_the_proposer(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('event_accept', args=[self.event.pk]))
        self.event.refresh_from_db()
        self.assertIsNone(self.event.proposed_by)
        self.assertTrue(self.event.accepting_requests)
        self.assertEqual(mail.outbox[-1].to, [self.proposer.email])

    def test_rejecting_removes_it_and_gives_the_reason(self):
        self.client.force_login(self.committee)
        self.client.post(reverse('event_reject', args=[self.event.pk]), {'reason': 'No budget this year'})
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())
        self.assertIn('No budget this year', mail.outbox[-1].body)

    def test_only_the_committee_decides(self):
        self.client.force_login(self.proposer)
        self.assertEqual(self.client.post(reverse('event_accept', args=[self.event.pk])).status_code, 403)
        self.event.refresh_from_db()
        self.assertIsNotNone(self.event.proposed_by)


class EventDeadlineTests(TestCase):
    """Deadlines have to make sense next to the event's own dates."""

    def form(self, **dates):
        data = {'name': 'Summit', 'location': 'India', 'start_date': '2027-12-20', 'end_date': '2027-12-23',
                'approval_percent': 100, **dates}
        return EventForm(data, sets_budget=False)

    def test_claims_cannot_close_before_the_event_ends(self):
        form = self.form(reimbursements_open_until='2026-12-05')
        self.assertIn('reimbursements_open_until', form.errors)

    def test_requests_cannot_stay_open_after_it(self):
        form = self.form(requests_open_until='2028-01-10')
        self.assertIn('requests_open_until', form.errors)

    def test_sensible_deadlines_pass(self):
        self.assertTrue(self.form(requests_open_until='2027-11-28', reimbursements_open_until='2028-01-31').is_valid())


class FinanceBudgetTests(TestCase):
    """Finance can change a budget after setting it, not only the first time."""

    def test_a_set_budget_can_be_changed_from_the_event_list(self):
        from profile.groups import GROUP_FINANCE
        from tsp.tests import make_user
        event = make_event()
        event.budget = 1000
        event.save()
        self.client.force_login(make_user('budgetfinance', [GROUP_FINANCE]))
        page = self.client.get(reverse('event_list'))
        self.assertContains(page, 'Change budget')
        response = self.client.post(reverse('event_edit', args=[event.pk]) + '?next=/events/',
                                    {'budget': '50000', 'budget_currency': 'EUR'})
        self.assertRedirects(response, '/events/', fetch_redirect_response=False)
        event.refresh_from_db()
        self.assertEqual(event.budget, 50000)


class RequestListTests(TestCase):
    """Requests in open and closed tabs, each person seeing only what they may."""

    def setUp(self):
        from tsp.models import RequestState, TravelRequest
        self.owner = make_requester('listowner')
        self.other = make_requester('listother')
        event = make_event()
        self.open_req = TravelRequest.objects.create(user=self.owner, event=event, state=RequestState.SUBMITTED)
        self.paid_req = TravelRequest.objects.create(user=self.owner, event=event, state=RequestState.PAID)
        self.someone_elses = TravelRequest.objects.create(user=self.other, event=event, state=RequestState.SUBMITTED)

    def test_open_and_closed_tabs_split_by_state(self):
        self.client.force_login(make_committee('listchair'))
        open_page = self.client.get(reverse('request_list'))
        self.assertContains(open_page, self.open_req.reference)
        self.assertNotContains(open_page, f'>{self.paid_req.reference}<')
        self.assertContains(open_page, 'Open <span class="badge badge-sm">2</span>', html=False)
        closed_page = self.client.get(reverse('request_list') + '?tab=closed')
        self.assertContains(closed_page, f'>{self.paid_req.reference}<')
        self.assertNotContains(closed_page, f'>{self.open_req.reference}<')

    def test_a_requester_sees_only_their_own(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse('request_list'))
        self.assertContains(page, f'>{self.open_req.reference}<')
        self.assertNotContains(page, f'>{self.someone_elses.reference}<')

    def test_the_status_filter_narrows_the_tab(self):
        self.client.force_login(make_committee('listfilter'))
        page = self.client.get(reverse('request_list') + '?tab=closed&status=rejected')
        self.assertContains(page, 'No closed requests with this status')
