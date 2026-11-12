from django.test import TestCase


class SettlementDetailTests(TestCase):
    """Finance needs what Wise actually did, not what was asked for."""

    def test_the_settled_amounts_rate_and_fee_are_captured(self):
        from payments.gateways.wise import settlement_details

        transfer = {
            'id': 1, 'status': 'outgoing_payment_sent', 'rate': 1.17928,
            'sourceCurrency': 'GBP', 'sourceValue': 24.33,
            'targetCurrency': 'EUR', 'targetValue': 28.00,
        }
        details = settlement_details(transfer, {'rate': 1.18, 'fee': 0.54})
        self.assertEqual(details['source_amount'], '24.33')
        self.assertEqual(details['target_amount'], '28.0')
        self.assertEqual(details['target_currency'], 'EUR')
        self.assertEqual(details['fee'], '0.54')

    def test_the_fee_comes_from_the_balance_option_of_a_v3_quote(self):
        from payments.gateways.wise import settlement_details

        quote = {'rate': 103.7, 'sourceCurrency': 'EUR', 'paymentOptions': [
            {'payIn': 'BANK_TRANSFER', 'payOut': 'BANK_TRANSFER', 'fee': {'total': 2.10}},
            {'payIn': 'BALANCE', 'payOut': 'BANK_TRANSFER', 'fee': {'total': 0.57}},
        ]}
        details = settlement_details({'sourceCurrency': 'EUR'}, quote)
        self.assertEqual((details['fee'], details['fee_currency']), ('0.57', 'EUR'))

    def test_the_transfer_rate_wins_over_the_quoted_one(self):
        from payments.gateways.wise import settlement_details

        # The quote is what was offered; the transfer is what happened.
        details = settlement_details({'rate': 1.17928}, {'rate': 1.25})
        self.assertEqual(details['exchange_rate'], '1.17928')

    def test_the_quoted_rate_is_used_when_the_transfer_has_none(self):
        from payments.gateways.wise import settlement_details

        details = settlement_details({'rate': 0.0}, {'rate': 1.25})
        self.assertEqual(details['exchange_rate'], '1.25')

    def test_missing_fields_are_left_out_rather_than_guessed(self):
        from payments.gateways.wise import settlement_details

        details = settlement_details({'status': 'processing'})
        self.assertNotIn('exchange_rate', details)
        self.assertNotIn('source_amount', details)
