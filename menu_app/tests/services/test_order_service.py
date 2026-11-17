from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections

from menu_app.models import Order, OrderItem
from menu_app.services import menu_utils, order_service


@pytest.mark.django_db
def test_place_order_rejects_empty_cart():
    with pytest.raises(ValueError, match='Cart cannot be empty'):
        order_service.place_order({})
    assert not Order.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_competing_checkouts_cannot_buy_the_same_stock(menu_item, inventory_item, monkeypatch):
    if connection.vendor != 'postgresql':
        pytest.skip('Concurrent checkout requires PostgreSQL')

    inventory_item.quantity = 1
    inventory_item.save()
    barrier = Barrier(2)
    original_check = menu_utils.check_item_availability

    def synchronized_check(item, quantity=1):
        available = original_check(item, quantity)
        # Both requests see the last item before either tries to deduct it.
        barrier.wait(timeout=10)
        return available

    monkeypatch.setattr(menu_utils, 'check_item_availability', synchronized_check)
    cart = {str(menu_item.id): 1}

    def checkout():
        close_old_connections()
        try:
            order_service.place_order(cart)
            return True
        except RuntimeError as exc:
            assert 'Insufficient stock' in str(exc)
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: checkout(), range(2)))

    assert sorted(results) == [False, True]
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 0
    assert Order.objects.count() == 1
    assert OrderItem.objects.count() == 1
