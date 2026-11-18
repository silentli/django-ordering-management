import pytest
from django.db.models.signals import pre_save
from django.urls import reverse

from menu_app.models import InventoryItem
from menu_app.services import inventory_service, order_service


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


@pytest.mark.django_db
def test_admin_cannot_change_order_status(admin_client, menu_item, inventory_item):
    placed_order = order_service.place_order({str(menu_item.pk): 2})
    change_url = reverse('admin:menu_app_order_change', args=[placed_order.pk])
    assert admin_client.post(change_url, {'status': 'cancelled'}).status_code == 403

    placed_order.refresh_from_db()
    inventory_item.refresh_from_db()
    assert placed_order.status == 'pending'
    assert inventory_item.quantity == 3
    assert placed_order.items.get().quantity == 2
