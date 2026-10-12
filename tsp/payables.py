"""TSP payable adapter for the payments plugin system."""

from decimal import Decimal

from django.urls import reverse

from payments.base import PayableAdapter, PaymentRequest
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
        """Return travel requests awaiting reimbursement."""
        return TravelRequest.objects.filter(
            state=RequestState.SUBMITTED_FOR_PAYMENT,
        ).select_related('user', 'event', 'user__profile')

    def get_object(self, object_id: int):
        """Load one travel request by primary key."""
        return TravelRequest.objects.select_related('user', 'event').get(pk=object_id)

    def build_payment_request(self, obj: TravelRequest) -> PaymentRequest:
        """
        Build a payment instruction from the payable total and user profile.

        Includes Wise ``wise_account_id`` in metadata when the requestor is verified.
        """
        user = obj.user
        totals = obj.totals_payable()
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
        return {
            'title': f'{obj.reference} — {obj.user.get_full_name() or obj.user.username}',
            'subtitle': f'{obj.event.name} · {obj.user.email}',
            'totals': obj.totals_payable(),
            'mixed_currencies': obj.has_mixed_currencies(),
            'banking': profile.banking_summary() if profile else 'Not configured',
            'wise_ready': wise_ready,
        }

    def payment_reference(self, obj) -> str:
        """Gateway reference string, the same one shown on the request."""
        return obj.reference
