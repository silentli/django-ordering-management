from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import close_old_connections, connection, connections, transaction

from menu_app.models import InventoryItem, Order
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
