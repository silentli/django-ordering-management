from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import close_old_connections, connection, connections, transaction

from menu_app.models import InventoryItem, MenuItem, Order
from menu_app.services import inventory_service, order_service


@pytest.mark.django_db(transaction=True)
def test_restock_and_checkout_preserve_both_changes(menu_item, inventory_item):
    if connection.vendor != 'postgresql':
        pytest.skip('Concurrent inventory updates require PostgreSQL')

    update_started = Event()

    def signal_update(execute, sql, params, many, context):
        if sql.startswith(f'UPDATE "{InventoryItem._meta.db_table}"'):
            update_started.set()
        return execute(sql, params, many, context)

    def restock():
        close_old_connections()
        try:
            with connection.execute_wrapper(signal_update):
                inventory_service.add_stock(menu_item, 2)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            order_service.place_order({str(menu_item.id): 1})
            # Restocking reads the old committed quantity, then waits for the
            # checkout's row lock. It must add to the quantity after checkout.
            future = executor.submit(restock)
            assert update_started.wait(timeout=10), 'Restocking did not reach its stock update'
        future.result(timeout=10)

    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 6
    assert Order.objects.count() == 1


@pytest.mark.django_db
def test_restock_creates_missing_inventory(menu_item):
    inventory_service.add_stock(menu_item, 3)
    assert InventoryItem.objects.get(menu_item=menu_item).quantity == 3


@pytest.mark.django_db
def test_setting_unchanged_stock_succeeds(menu_item, inventory_item):
    inventory_service.set_stock(menu_item, 5, expected_quantity=5)
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 5


@pytest.mark.django_db
def test_bulk_stock_changes_distinguish_business_failures_from_unexpected_errors(
    menu_item, inventory_item, monkeypatch
):
    other_item = MenuItem.objects.create(name='Other item', category='main', price='2.00')
    other_stock = InventoryItem.objects.create(menu_item=other_item, quantity=5)
    result = inventory_service.bulk_update_inventory({menu_item.pk: 2, other_item.pk: -6})
    assert result == {menu_item.pk: True, other_item.pk: False}

    update_inventory = inventory_service.update_inventory

    def failing_update(item, change):
        update_inventory(item, change)
        if item.pk == other_item.pk:
            raise RuntimeError('Unexpected write failure')

    monkeypatch.setattr(inventory_service, 'update_inventory', failing_update)
    with pytest.raises(RuntimeError):
        inventory_service.bulk_update_inventory({menu_item.pk: 1, other_item.pk: 1})

    inventory_item.refresh_from_db()
    other_stock.refresh_from_db()
    assert inventory_item.quantity == 7
    assert other_stock.quantity == 5
