from django import forms

from payments.registry import get_all_gateways


class ProcessPaymentForm(forms.Form):
    object_id = forms.IntegerField(widget=forms.HiddenInput())
    gateway_id = forms.ChoiceField(
        widget=forms.Select(attrs={'class': 'select select-bordered select-sm'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['gateway_id'].choices = [
            (gw.gateway_id, gw.label) for gw in get_all_gateways()
        ]


class BatchExportForm(forms.Form):
    gateway_id = forms.CharField(widget=forms.HiddenInput())
