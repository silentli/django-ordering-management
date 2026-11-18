import pytest
from django.db.models.signals import pre_save
from django.urls import reverse

from menu_app.models import InventoryItem
from menu_app.services import inventory_service


@pytest.mark.django_db
def test_admin_edit_preserves_stock_changes(admin_client, menu_item, inventory_item):
    def checkout_before_save(sender, instance, **kwargs):
        inventory_service.remove_stock(menu_item, 1)

    # Schedule a sale after the admin has read the record, just before saving.
    pre_save.connect(checkout_before_save, sender=InventoryItem)
    try:
        response = admin_client.post(
            reverse('admin:menu_app_inventoryitem_change', args=[inventory_item.pk]),
            {'menu_item': menu_item.pk, 'low_stock_threshold': 10, 'quantity': 100},
        )
    finally:
        pre_save.disconnect(checkout_before_save, sender=InventoryItem)

    assert response.status_code == 302
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 4
    assert inventory_item.low_stock_threshold == 10
