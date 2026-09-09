"""TSP payable adapter for the payments plugin system."""

from decimal import Decimal

from django.urls import reverse

from payments.base import PayableAdapter, PaymentRequest
from tsp.models import RequestState, TravelRequest
from tsp.services.state_machine import StateTransitionError, transition_request


class TravelRequestPayableAdapter(PayableAdapter):
    adapter_id = 'tsp.travelrequest'
    label = 'Travel Support Requests'

    def get_queue(self):
        return TravelRequest.objects.filter(
            state=RequestState.SUBMITTED_FOR_PAYMENT,
        ).select_related('user', 'event', 'user__profile')

    def get_object(self, object_id: int):
        return TravelRequest.objects.select_related('user', 'event').get(pk=object_id)

    def build_payment_request(self, obj: TravelRequest) -> PaymentRequest:
        user = obj.user
        currency = 'EUR'
        first_expense = obj.expenses.first()
        if first_expense and first_expense.approved_currency:
            currency = first_expense.approved_currency

        profile = getattr(user, 'profile', None)
        metadata = {}
        if profile:
            metadata['banking_summary'] = profile.banking_summary()
            metadata['banking_method'] = profile.banking_method or ''
            if profile.wise_account_id:
                metadata['wise_account_id'] = profile.wise_account_id

        return PaymentRequest(
            amount=Decimal(str(obj.total_approved())),
            currency=currency,
            recipient_name=user.get_full_name() or user.username,
            recipient_email=user.email,
            reference=self.payment_reference(obj),
            metadata=metadata,
        )

    def on_payment_completed(self, obj, payment_record, user) -> None:
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
        return reverse('request_detail', kwargs={'pk': obj.pk})

    def get_queue_item_context(self, obj: TravelRequest) -> dict:
        profile = getattr(obj.user, 'profile', None)
        wise_ready = bool(profile and profile.is_wise_recipient_ready())
        return {
            'title': f'#{obj.pk} — {obj.user.get_full_name() or obj.user.username}',
            'subtitle': f'{obj.event.name} · {obj.user.email}',
            'amount': obj.total_approved(),
            'currency': obj.expenses.first().approved_currency if obj.expenses.exists() else 'EUR',
            'banking': profile.banking_summary() if profile else 'Not configured',
            'wise_ready': wise_ready,
        }

    def payment_reference(self, obj) -> str:
        return f'TSP-{obj.pk}'
