"""Remind people who have not claimed yet that their window is closing."""

from django.core.management.base import BaseCommand

from profile.services.email_notifications import send_to
from tsp.models import RequestState, TravelRequest

# Remind at these many days left, so someone gets a nudge in good time and
# again near the end rather than a message every day.
REMINDER_DAYS = (14, 7, 1)


class Command(BaseCommand):
    help = 'Email requesters whose reimbursement deadline is approaching'

    def add_arguments(self, parser):
        """Allow a dry run, and overriding which days get a reminder."""
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List who would be emailed without sending anything.',
        )
        parser.add_argument(
            '--days', type=int, nargs='+', default=list(REMINDER_DAYS),
            help='Days remaining that trigger a reminder.',
        )

    def handle(self, *args, **options):
        """Find accepted requests whose event closes soon and nudge the owner."""
        days = set(options['days'])
        dry_run = options['dry_run']

        # Accepted means the trip is agreed but nothing has been claimed yet.
        pending = TravelRequest.objects.filter(
            state=RequestState.ACCEPTED,
        ).select_related('user', 'event')

        sent = 0
        for travel_request in pending:
            left = travel_request.event.days_left_to_claim()
            if left not in days:
                continue
            sent += 1
            self.stdout.write(
                f'{travel_request.reference if hasattr(travel_request, "reference") else travel_request.pk}: '
                f'{travel_request.user.email}, {left} day(s) left'
            )
            if dry_run:
                continue
            send_to(
                [travel_request.user],
                subject=f'{left} day(s) left to claim for {travel_request.event.name}',
                headline='Your reimbursement window is closing',
                travel_request=travel_request,
                notes=(
                    f'Claims for {travel_request.event.name} close on '
                    f'{travel_request.event.reimbursement_deadline()}. Upload your '
                    f'receipts and report what the trip cost before then.'
                ),
            )

        self.stdout.write(self.style.SUCCESS(
            f'{"Would remind" if dry_run else "Reminded"} {sent} requester(s).'
        ))
