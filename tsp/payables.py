"""TSP payable adapter for the payments plugin system."""

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Exists, OuterRef, Q
from django.urls import reverse

from payments.base import PayableAdapter, PaymentRequest
from payments.models import PaymentRecord
from tsp.models import RequestState, TravelRequest
from tsp.services.state_machine import StateTransitionError, transition_request


class TravelRequestPayableAdapter(PayableAdapter):
    """
    Connects approved travel requests to the payments app queue.

    Finance processes reimbursements for requests in ``submitted_for_payment``
    state; successful payment transitions them to ``paid``.
    """

    adapter_id = 'tsp.travelrequest'
    label = 'Travel Support Requests'

    def get_queue(self):
        """Travel requests awaiting reimbursement, or an advance before the event."""
        open_advance = PaymentRecord.objects.filter(
            content_type=ContentType.objects.get_for_model(TravelRequest),
            object_id=OuterRef('pk'),
            purpose='advance',
        ).exclude(status__in=['failed', 'cancelled'])
        return TravelRequest.objects.filter(
            Q(state=RequestState.SUBMITTED_FOR_PAYMENT)
            | Q(state=RequestState.ACCEPTED, advance_requested_at__isnull=False)
            & ~Exists(open_advance)
        ).select_related('user', 'event', 'user__profile')

    def payment_purpose(self, obj) -> str:
        return 'advance' if obj.state == RequestState.ACCEPTED else 'payment'

    def get_object(self, object_id: int):
        """Load one travel request by primary key."""
        return TravelRequest.objects.select_related('user', 'event').get(pk=object_id)

    def build_payment_request(self, obj: TravelRequest) -> PaymentRequest:
        """
        Build a payment instruction from the payable total and user profile.

        Includes Wise ``wise_account_id`` in metadata when the requestor is verified.
        """
        user = obj.user
        if self.payment_purpose(obj) == 'advance':
            totals = [(obj.advance_currency, obj.advance_amount)]
        else:
            # Whatever the advance already paid comes off the settlement.
            totals = obj.amounts_to_pay()
            if not totals and obj.advance_payment():
                raise ValueError(
                    f'The advance already covers request #{obj.pk}. '
                    'Close it from the request page instead of paying it.'
                )
        if len(totals) > 1:
            # Taking the first expense's currency used to label the whole sum,
            # so a 3 USD line was paid as 3 EUR. One transfer moves one
            # currency, so this needs a person, not a guess.
            mixed = ', '.join(f'{amount} {code}' for code, amount in totals)
            raise ValueError(
                f'Request #{obj.pk} mixes currencies ({mixed}). '
                'Pay each currency separately or re-approve it in one currency.'
            )
        currency, amount = totals[0] if totals else ('EUR', Decimal('0'))

        profile = getattr(user, 'profile', None)
        metadata = {}
        if profile:
            metadata['banking_summary'] = profile.banking_summary()
            metadata['banking_method'] = profile.banking_method or ''
            if profile.wise_account_id:
                metadata['wise_account_id'] = profile.wise_account_id
            metadata['account_currency'] = profile.banking_currency

        return PaymentRequest(
            amount=Decimal(str(amount)),
            currency=currency,
            recipient_name=user.get_full_name() or user.username,
            recipient_email=user.email,
            reference=self.payment_reference(obj),
            metadata=metadata,
        )

    def on_payment_completed(self, obj, payment_record, user) -> None:
        """Transition the travel request to ``paid`` after a successful gateway payment."""
        if payment_record.purpose == 'advance':
            # The request carries on: the trip still has to happen.
            return
        try:
            gateway_label = payment_record.method_label
            transition_request(
                obj,
                RequestState.PAID,
                user,
                notes=f'Payment via {gateway_label}',
            )
        except StateTransitionError:
            pass

    def get_detail_url(self, obj) -> str:
        """URL name for linking from the finance payment dashboard."""
        return reverse('request_detail', kwargs={'pk': obj.pk})

    def get_queue_item_context(self, obj: TravelRequest) -> dict:
        """Extra template context for one item on the finance payment queue."""
        profile = getattr(obj.user, 'profile', None)
        wise_ready = bool(profile and profile.is_wise_recipient_ready())
        name = obj.user.get_full_name() or obj.user.username
        if self.payment_purpose(obj) == 'advance':
            return {
                'title': f'#{obj.pk} — {name} · Advance before the event',
                'subtitle': f'{obj.event.name} ({obj.event.start_date:%d %b %Y}) · {obj.user.email}',
                'totals': [(obj.advance_currency, obj.advance_amount)],
                'banking': profile.banking_summary() if profile else 'Not configured',
                'wise_ready': wise_ready,
            }
        advance = obj.advance_payment()
        return {
            'title': f'{obj.reference} · {name}',
            'subtitle': (
                f'{obj.event.name} · {obj.user.email}'
                + (f' · advance of {advance.amount} {advance.currency} already paid' if advance else '')
            ),
            'totals': obj.amounts_to_pay(),
            'mixed_currencies': obj.has_mixed_currencies(),
            'banking': profile.banking_summary() if profile else 'Not configured',
            'wise_ready': wise_ready,
        }

    def payment_reference(self, obj) -> str:
        """Gateway reference string, the same one shown on the request."""
        if self.payment_purpose(obj) == 'advance':
            return f'{obj.reference}-ADVANCE'
        return obj.reference
