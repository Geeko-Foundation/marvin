from decimal import Decimal
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from payments.models import PaymentRecord
from payments.services import PaymentService
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP
from profile.services.wise_recipient import RETURN_TO_SESSION_KEY
from profile.services.wise_verification import SESSION_KEY
from tsp.models import Expense, RequestState, TravelRequest
from tsp.tests import make_event, make_user


def paid_request(owner, state=RequestState.PAID):
    return TravelRequest.objects.create(user=owner, event=make_event(), state=state, description='T')


def payment_for(req, **fields):
    values = dict(
        content_type=ContentType.objects.get_for_model(req), object_id=req.pk, gateway_id='wise-api',
        amount=Decimal('10.00'), currency='EUR', status='success', reference=req.reference,
        recipient_name='x', recipient_email='x@example.org',
    )
    values.update(fields)
    return PaymentRecord.objects.create(**values)


class BankingRedirectTests(TestCase):
    """Finishing banking setup only ever leads back into the site."""

    def setUp(self):
        self.client.force_login(make_user('returner', [GROUP_REQUESTER]))

    def stored_after(self, return_to):
        self.client.get(reverse('signup_banking'), {'return_to': return_to})
        return self.client.session.get(RETURN_TO_SESSION_KEY)

    def test_a_link_elsewhere_or_a_script_is_not_kept(self):
        self.assertIsNone(self.stored_after('javascript:alert(document.cookie)'))
        self.assertIsNone(self.stored_after('https://evil.example/login'))

    def test_a_page_on_this_site_is_kept(self):
        self.assertEqual(self.stored_after('/requests/new/'), '/requests/new/')


class SandboxPaymentTests(TestCase):
    """A sandbox transfer is never shown as real money paid."""

    def test_the_payment_page_warns_while_wise_is_in_sandbox(self):
        self.client.force_login(make_user('sandboxfinance', [GROUP_FINANCE]))
        with override_settings(WISE_API_SANDBOX=True):
            self.assertContains(self.client.get(reverse('payments:dashboard')), 'move no real money')
        with override_settings(WISE_API_SANDBOX=False):
            self.assertNotContains(self.client.get(reverse('payments:dashboard')), 'move no real money')

    def test_the_history_says_no_money_was_sent(self):
        from tsp.payables import TravelRequestPayableAdapter

        finance = make_user('sandboxpayer', [GROUP_FINANCE])
        req = paid_request(make_user('sandboxowner', [GROUP_REQUESTER]), RequestState.SUBMITTED_FOR_PAYMENT)
        record = payment_for(req, metadata={'sandbox': True}, processed_by=finance)
        TravelRequestPayableAdapter().on_payment_completed(req, record, finance)
        self.assertIn('sandbox, no money sent', req.state_history.get(to_state=RequestState.PAID).notes)


class ApprovalAmountTests(TestCase):
    """Every line needs an amount the committee actually typed."""

    def setUp(self):
        self.committee = make_user('amountcommittee', [GROUP_TSP])
        self.req = paid_request(make_user('amountowner', [GROUP_REQUESTER]), RequestState.SUBMITTED)
        self.expense = Expense.objects.create(
            request=self.req, category='flight', description='F',
            estimated_amount=Decimal('100'), estimated_currency='EUR')

    def approve(self, amount):
        self.client.force_login(self.committee)
        return self.client.post(reverse('request_approve', args=[self.req.pk]), {
            'expense_id': [str(self.expense.pk)],
            f'approved_amount_{self.expense.pk}': amount,
            f'approved_currency_{self.expense.pk}': 'EUR',
        }, follow=True)

    def test_a_blank_or_unreadable_amount_refuses_the_approval(self):
        for amount in ('', '73,62', 'abc'):
            with self.subTest(amount=amount):
                response = self.approve(amount)
                self.req.refresh_from_db()
                self.assertEqual(self.req.state, RequestState.SUBMITTED)
                self.assertContains(response, 'Enter an approved amount for Flight')


class SettledTotalsTests(TestCase):
    """Only money that actually went out counts as paid."""

    def test_failed_payments_and_exports_are_left_out(self):
        req = paid_request(make_user('totalsowner', [GROUP_REQUESTER]))
        payment_for(req, amount=Decimal('33'), status='failed')
        payment_for(req, amount=Decimal('50'), status='exported', gateway_id='wise-csv')
        payment_for(req, amount=Decimal('40'), status='success')
        self.assertEqual(req.event.paid_by_currency(), [('EUR', Decimal('40.00'))])
        self.assertEqual(req.settled_payment.amount, Decimal('40.00'))

        self.client.force_login(make_user('totalsfinance', [GROUP_FINANCE]))
        csv = self.client.get(reverse('reports'), {'format': 'csv'}).content.decode()
        self.assertIn(',40.00,EUR,', csv)
        self.assertNotIn(',33.00,', csv)


class ReturnedPaymentTests(TestCase):
    """Money that bounces back puts the request back in front of finance."""

    def test_a_bounced_transfer_reopens_the_request_and_alerts_finance(self):
        finance = make_user('bouncefinance', [GROUP_FINANCE])
        req = paid_request(make_user('bounceowner', [GROUP_REQUESTER]))
        record = payment_for(req, external_id='77', processed_by=finance)

        from payments.base import PaymentResult
        gateway = mock.Mock()
        gateway.get_status.return_value = PaymentResult(
            status='failed', external_id='77', metadata={'transfer_status': 'bounced_back'})
        with mock.patch('payments.services.get_gateway', return_value=gateway), \
                mock.patch('profile.services.push_notifications.send_push_to_group') as push:
            PaymentService().refresh_status(record)

        req.refresh_from_db()
        self.assertEqual(req.state, RequestState.SUBMITTED_FOR_PAYMENT)
        self.assertIn('bounced_back', req.state_history.latest('id').notes)
        push.assert_called_once()
        self.assertIsNone(req.settled_payment)

    def test_bounce_states_from_wise_count_as_failed(self):
        from payments.gateways.wise import TRANSFER_STATUS_MAP

        for state in ('funds_refunded', 'bounced_back', 'charged_back'):
            self.assertEqual(TRANSFER_STATUS_MAP[state], 'failed')


class PasswordResetTests(TestCase):
    """Someone who forgets their password can get back in without an admin."""

    def test_the_whole_reset_works_from_the_emailed_link(self):
        user = make_user('forgetful', [GROUP_REQUESTER])
        self.assertContains(self.client.get(reverse('login')), reverse('password_reset'))
        self.client.post(reverse('password_reset'), {'email': user.email})
        self.assertEqual(len(mail.outbox), 1)
        link = next(line for line in mail.outbox[0].body.splitlines() if '/password/reset/' in line)
        path = link.split('testserver', 1)[1]
        form_page = self.client.get(path, follow=True)
        self.assertContains(form_page, 'Choose a new password')
        self.client.post(form_page.redirect_chain[-1][0], {
            'new_password1': 'a-Fresh-pass-phrase-7', 'new_password2': 'a-Fresh-pass-phrase-7'})
        user.refresh_from_db()
        self.assertTrue(user.check_password('a-Fresh-pass-phrase-7'))

    def test_an_unknown_address_says_the_same_and_sends_nothing(self):
        response = self.client.post(reverse('password_reset'), {'email': 'nobody@example.org'}, follow=True)
        self.assertContains(response, 'Check your email')
        self.assertEqual(len(mail.outbox), 0)


class LoginLockoutTests(TestCase):
    """Guessing from one place does not lock the owner out everywhere."""

    def setUp(self):
        cache.clear()
        self.user = make_user('lockowner', [GROUP_REQUESTER])
        self.user.set_password('right-pass-phrase-9')
        self.user.save()

    def attempt(self, password, address):
        return self.client.post(reverse('login'), {'username': 'lockowner', 'password': password},
                                REMOTE_ADDR=address)

    def test_the_owner_signs_in_from_elsewhere_while_a_guesser_is_locked(self):
        for _ in range(10):
            self.attempt('wrong', '203.0.113.9')
        self.assertContains(self.attempt('right-pass-phrase-9', '203.0.113.9'), 'Too many failed')
        self.assertEqual(self.attempt('right-pass-phrase-9', '198.51.100.4').status_code, 302)


class EventFilterTests(TestCase):
    def test_a_date_that_is_not_a_date_is_ignored(self):
        self.client.force_login(make_user('filterer', [GROUP_REQUESTER]))
        for value in ('abc', '2026-13-45'):
            with self.subTest(value=value):
                self.assertEqual(self.client.get(reverse('event_list'), {'from': value}).status_code, 200)


class BankingSessionTests(TestCase):
    """Bank details typed in for checking do not stay in the session."""

    def setUp(self):
        self.client.force_login(make_user('sessionowner', [GROUP_REQUESTER], onboarded=False))

    def seed(self, started_at):
        session = self.client.session
        session[SESSION_KEY] = {'banking': {'bank_iban': 'DE89370400440532013000'}, 'started_at': started_at}
        session.save()

    def test_opening_the_banking_form_again_drops_an_abandoned_attempt(self):
        import time
        self.seed(time.time())
        self.client.get(reverse('signup_banking'))
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_an_attempt_older_than_an_hour_is_dropped(self):
        self.seed(0)
        self.client.post(reverse('signup_banking_step'), {'step': 'create_payee'})
        self.assertNotIn(SESSION_KEY, self.client.session)


class TransitionMessageTests(TestCase):
    def test_a_refused_change_names_the_states_in_words(self):
        from tsp.services.state_machine import StateTransitionError, transition_request

        req = paid_request(make_user('wordsowner', [GROUP_REQUESTER]))
        with self.assertRaises(StateTransitionError) as raised:
            transition_request(req, RequestState.CANCELLED, req.user)
        self.assertEqual(str(raised.exception), f'{req.reference} cannot go from Paid to Cancelled.')


class RequestActionsPlacementTests(TestCase):
    """The request's actions follow its details, so they are read before acting."""

    def test_actions_come_after_the_expenses_and_documents(self):
        owner = make_user('placementowner', [GROUP_REQUESTER])
        req = paid_request(owner, RequestState.DRAFT)
        self.client.force_login(owner)
        html = self.client.get(reverse('request_detail', args=[req.pk])).content.decode()
        self.assertLess(html.index('>Documents</h2>'), html.index('>Submit for Review<'))
        self.assertLess(html.index('>Submit for Review<'), html.index('>Comments</h2>'))
