from datetime import date
from unittest import mock

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from tsp.models import Event, RequestState, TravelRequest
from tsp.services.state_machine import transition_request
from tsp.tests import make_committee, make_event, make_requester

BROKEN_MAIL = mock.patch('django.core.mail.EmailMessage.send', side_effect=OSError('mail server down'))


def proposal(proposer):
    return Event.objects.create(
        name='Asia Summit', location='India', start_date=date(2027, 12, 20), end_date=date(2027, 12, 23),
        accepting_requests=False, proposed_by=proposer,
    )


class LinksInEmailsTests(TestCase):
    """Every email about a change points straight at the page to act on."""

    def test_a_request_update_links_to_the_request(self):
        committee = make_committee('linkcommittee')
        committee.email = 'committee@example.org'
        committee.save()
        request = TravelRequest.objects.create(user=make_requester('linkowner'), event=make_event())
        transition_request(request, RequestState.SUBMITTED, request.user)
        message = mail.outbox[-1]
        self.assertIn(request.reference, message.subject)
        self.assertIn(reverse('request_detail', args=[request.pk]), message.body)
        self.assertIn('Hello', message.body)

    def test_an_accepted_event_links_to_applying(self):
        event = proposal(make_requester('linkproposer'))
        self.client.force_login(make_committee('linkchair'))
        self.client.post(reverse('event_accept', args=[event.pk]))
        self.assertIn(reverse('request_create') + f'?event={event.pk}', mail.outbox[-1].body)

    def test_the_committee_is_emailed_a_new_proposal(self):
        chair = make_committee('proposalchair')
        chair.email = 'chair@example.org'
        chair.save()
        self.client.force_login(make_requester('proposalauthor'))
        self.client.post(reverse('event_create'), {
            'name': 'New Summit', 'location': 'Online', 'start_date': '2027-09-01', 'end_date': '2027-09-02',
        })
        message = next(m for m in mail.outbox if m.to == ['chair@example.org'])
        self.assertIn('New Summit', message.subject)
        self.assertIn(reverse('event_list'), message.body)


class MailFailureTests(TestCase):
    """A mail server being down never shows an error page; people are told what happened instead."""

    def test_accepting_still_works_and_says_the_email_failed(self):
        event = proposal(make_requester('downproposer'))
        self.client.force_login(make_committee('downchair'))
        with BROKEN_MAIL:
            response = self.client.post(reverse('event_accept', args=[event.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'could not be emailed')
        event.refresh_from_db()
        self.assertTrue(event.accepting_requests)

    def test_a_proposal_already_decided_says_so_instead_of_an_error(self):
        event = proposal(make_requester('twiceproposer'))
        self.client.force_login(make_committee('twicechair'))
        self.client.post(reverse('event_accept', args=[event.pk]))
        response = self.client.post(reverse('event_reject', args=[event.pk]), follow=True)
        self.assertContains(response, 'already been reviewed')
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())

    def test_sign_up_with_mail_down_explains_and_offers_to_resend(self):
        with BROKEN_MAIL:
            response = self.client.post(reverse('signup'), {
                'first_name': 'Ada', 'email': 'ada@example.org',
                'password1': 'a-long-unusual-passphrase', 'password2': 'a-long-unusual-passphrase',
            })
        self.assertContains(response, 'could not send the email')
        self.assertContains(response, 'Send the link again')

    def test_a_password_reset_with_mail_down_does_not_crash(self):
        make_requester('resetowner')
        with BROKEN_MAIL:
            response = self.client.post(reverse('password_reset'), {'email': 'resetowner@example.org'})
        self.assertEqual(response.status_code, 302)

    def test_a_broken_email_change_link_is_explained(self):
        self.client.force_login(make_requester('linkbroken'))
        response = self.client.get(reverse('email_change_confirm', args=['not-a-real-token']), follow=True)
        self.assertContains(response, 'expired or was already used')
