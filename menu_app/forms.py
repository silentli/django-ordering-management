from django import forms

from menu_app.models.menu_item import MenuItem


class InventoryStockForm(forms.Form):
    quantity = forms.IntegerField(min_value=0)
    expected_quantity = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class MenuForm(forms.ModelForm):
    class Meta:
        model = MenuItem
        fields = ['name', 'price', 'category', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'category': forms.Select(attrs={'class': 'form-control'}),
        }
