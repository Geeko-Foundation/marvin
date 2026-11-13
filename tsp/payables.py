"""TSP payable adapter for the payments plugin system."""

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Exists, OuterRef, Q
from django.urls import reverse

from payments.base import PayableAdapter, PaymentRequest
from payments.models import PaymentRecord
from profile.groups import GROUP_FINANCE
from tsp.models import RequestState, TravelRequest
from tsp.services.state_machine import (
    StateTransitionError,
    reopen_for_payment,
    transition_request,
)


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
                    f'The advance already covers {obj.reference}. '
                    'Close it from the request page instead of paying it.'
                )
        profile = getattr(user, 'profile', None)
        conversion = {}
        if len(totals) > 1:
            # One transfer moves one currency. A single currency is converted
            # into the bank account's by the gateway at Wise's own rate.
            currency, amount, conversion = obj.in_payout_currency(totals)
        else:
            currency, amount = totals[0] if totals else ('EUR', Decimal('0'))

        metadata = {'converted_from': conversion} if conversion else {}
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
            # A sandbox transfer moves no money, so the history must not read as paid for real.
            sandbox = ' (sandbox, no money sent)' if payment_record.metadata.get('sandbox') else ''
            transition_request(
                obj,
                RequestState.PAID,
                user,
                notes=f'Payment via {payment_record.method_label}{sandbox}',
            )
        except StateTransitionError:
            pass

    def on_payment_returned(self, obj, payment_record) -> None:
        """Back to the payment queue, with finance told why, when the money came back."""
        from profile.services.push_notifications import send_push_to_group

        message = (
            f'Wise returned the {payment_record.amount} {payment_record.currency} sent for '
            f'{obj.reference} ({payment_record.metadata.get("transfer_status", "failed")}). '
            'The requester has not been paid. Check their bank details and pay them again.'
        )
        if payment_record.purpose != 'advance' and payment_record.processed_by:
            try:
                reopen_for_payment(obj, payment_record.processed_by, message)
            except StateTransitionError:
                pass
        send_push_to_group(GROUP_FINANCE, head='Payment returned', body=message, url=self.get_detail_url(obj))

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
                'title': f'{obj.reference} · {name} · Advance before the event',
                'subtitle': f'{obj.event.name} ({obj.event.start_date:%d %b %Y}) · {obj.user.email}',
                'totals': [(obj.advance_currency, obj.advance_amount)],
                'converted': obj.payout_preview([(obj.advance_currency, obj.advance_amount)]),
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
            'converted': obj.payout_preview(),
            'banking': profile.banking_summary() if profile else 'Not configured',
            'wise_ready': wise_ready,
        }

    def payment_reference(self, obj) -> str:
        """Gateway reference string, the same one shown on the request."""
        if self.payment_purpose(obj) == 'advance':
            return f'{obj.reference}-ADVANCE'
        return obj.reference
