from unittest import mock

from django.core import mail
from django.test import TestCase

from profile.groups import GROUP_FINANCE
from tsp.models import RequestState, TravelRequest
from tsp.services.state_machine import transition_request
from tsp.tests import make_committee, make_event, make_requester, make_user


class EmailNotificationTests(TestCase):
    def setUp(self):
        self.owner = make_requester('mailowner')
        self.committee = make_committee('mailcommittee')
        self.committee.email = 'committee@example.org'
        self.committee.save()
        self.finance = make_user('mailfinance', [GROUP_FINANCE])
        self.request = TravelRequest.objects.create(
            user=self.owner, event=make_event(), description='Talk',
        )
        mail.outbox = []

    def test_submitting_emails_the_committee_not_the_requester(self):
        transition_request(self.request, RequestState.SUBMITTED, self.owner)
        recipients = [address for message in mail.outbox for address in message.to]
        self.assertIn(self.committee.email, recipients)
        self.assertNotIn(self.owner.email, recipients)

    def test_approving_emails_the_requester(self):
        transition_request(self.request, RequestState.SUBMITTED, self.owner)
        mail.outbox = []
        transition_request(self.request, RequestState.APPROVED, self.committee, 'Approved at a lower amount')
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn('approved', message.subject.lower())
        self.assertIn('Approved at a lower amount', message.body)

    def test_the_email_links_to_the_request(self):
        transition_request(self.request, RequestState.SUBMITTED, self.owner)
        mail.outbox = []
        transition_request(self.request, RequestState.APPROVED, self.committee)
        self.assertIn(f'/requests/{self.request.pk}/', mail.outbox[0].body)

    def test_ready_for_payment_emails_finance(self):
        for state in (RequestState.SUBMITTED, RequestState.APPROVED, RequestState.ACCEPTED):
            transition_request(self.request, state, self.committee)
        mail.outbox = []
        transition_request(self.request, RequestState.SUBMITTED_FOR_PAYMENT, self.owner)
        recipients = [address for message in mail.outbox for address in message.to]
        self.assertEqual(recipients, [self.finance.email])

    def test_a_user_without_an_address_is_skipped(self):
        self.owner.email = ''
        self.owner.save()
        transition_request(self.request, RequestState.SUBMITTED, self.owner)
        mail.outbox = []
        transition_request(self.request, RequestState.APPROVED, self.committee)
        self.assertEqual(mail.outbox, [])

    def test_a_broken_template_does_not_block_the_transition(self):
        transition_request(self.request, RequestState.SUBMITTED, self.owner)
        with mock.patch('profile.services.email_notifications.render_to_string',
                        side_effect=RuntimeError('template blew up')):
            with self.assertLogs('marvin.email', level='ERROR'):
                transition_request(self.request, RequestState.APPROVED, self.committee)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.APPROVED)

    def test_a_broken_mail_server_does_not_block_the_transition(self):
        transition_request(self.request, RequestState.SUBMITTED, self.owner)
        with mock.patch('profile.services.email_notifications.EmailMessage.send',
                        side_effect=OSError('smtp is down')):
            with self.assertLogs('marvin.email', level='ERROR') as logged:
                transition_request(self.request, RequestState.APPROVED, self.committee)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.APPROVED)
        self.assertIn('smtp is down', '\n'.join(logged.output))
