"""Management command to exercise the Wise API recipient verification flow."""

from django.core.management.base import BaseCommand

from payments.gateways.wise_api import WiseApiError, get_wise_client


class Command(BaseCommand):
    help = 'Run the Wise recipient verification flow (create payee → bank details → test → reverse)'

    def add_arguments(self, parser):
        parser.add_argument('--name', default='Douglas DeMaio', help='Recipient full name')
        parser.add_argument('--email', default='douglas@example.com', help='Recipient email')
        parser.add_argument('--currency', default='EUR', help='Target currency')
        parser.add_argument('--iban', default='DE89370400440532013000', help='IBAN')
        parser.add_argument('--bic', default='COBADEFFXXX', help='BIC/SWIFT')
        parser.add_argument('--amount', default='1.00', help='Test transfer amount (source currency when cross-border)')
        parser.add_argument('--city', default='Berlin', help='Recipient city')
        parser.add_argument('--address', default='1 Example Street', help='Recipient address line')
        parser.add_argument('--post-code', default='10115', help='Recipient post code')

    def handle(self, *args, **options):
        client = get_wise_client()

        self.stdout.write(
            f'Using Wise API (mode={client.mode_label}, profile={client.resolve_profile_id()})'
        )

        try:
            from django.conf import settings
            result = client.verify_recipient(
                full_name=options['name'],
                email=options['email'],
                currency=options['currency'],
                iban=options['iban'],
                bic=options['bic'],
                source_currency=getattr(settings, 'WISE_SOURCE_CURRENCY', 'GBP'),
                test_amount=options['amount'],
                country=options['iban'][:2].upper() if options.get('iban') else None,
                address_line=options['address'],
                city=options['city'],
                post_code=options['post_code'],
            )
        except WiseApiError as exc:
            self.stderr.write(self.style.ERROR(f'Wise API error ({exc.status_code}): {exc}'))
            return

        self.stdout.write(self.style.SUCCESS('Recipient verification complete'))
        self.stdout.write(f'  Account ID:      {result["account_id"]}')
        self.stdout.write(f'  Verified:        {result["verified"]}')
        self.stdout.write(f'  Test transfer:   {result["test_transfer_id"]} ({result["test_transfer_status"]})')
        self.stdout.write(f'  Reversal status: {result["reversal_status"]}')
