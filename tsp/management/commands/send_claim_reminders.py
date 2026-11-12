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
            # The tightest reminder now due. A missed run catches up on the
            # next one, and a second run on the same day finds nothing new.
            due = min((d for d in days if 0 <= left <= d), default=None)
            already = travel_request.claim_reminder_days
            if due is None or (already is not None and already <= due):
                continue
            sent += 1
            self.stdout.write(
                f'{travel_request.reference}: {travel_request.user.email}, {left} day(s) left'
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
            travel_request.claim_reminder_days = due
            travel_request.save(update_fields=['claim_reminder_days'])

        self.stdout.write(self.style.SUCCESS(
            f'{"Would remind" if dry_run else "Reminded"} {sent} requester(s).'
        ))
