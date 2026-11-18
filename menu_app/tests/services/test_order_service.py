from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, connections, transaction

from menu_app.models import InventoryItem, MenuItem, Order, OrderItem
from menu_app.services import inventory_service, menu_service, menu_utils, order_service


@pytest.fixture
def placed_order(menu_item, inventory_item):
    return order_service.place_order({str(menu_item.pk): 2})


@pytest.fixture
def staff_actor(django_user_model):
    return django_user_model.objects.create_user(username='order-staff', is_staff=True)


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


@pytest.mark.django_db
@pytest.mark.parametrize('action', ['complete', 'cancel'])
def test_terminal_order_rejects_further_transitions(
    placed_order, inventory_item, action, staff_actor
):
    stale_order = Order.objects.get(pk=placed_order.pk)
    if action == 'cancel':
        placed_order.cancel(actor=staff_actor)
    else:
        placed_order.complete()

    with pytest.raises(ValueError):
        stale_order.cancel(actor=staff_actor)
    with pytest.raises(ValueError):
        stale_order.complete()

    placed_order.refresh_from_db()
    inventory_item.refresh_from_db()
    assert placed_order.status == {'complete': 'completed', 'cancel': 'cancelled'}[action]
    assert inventory_item.quantity == (5 if action == 'cancel' else 3)
    assert placed_order.items.get().quantity == 2


@pytest.mark.django_db
def test_cancellation_rolls_back_all_restored_stock(
    menu_item, inventory_item, monkeypatch, staff_actor
):
    other_item = MenuItem.objects.create(name='Other item', category='beverage', price='2.50')
    other_stock = InventoryItem.objects.create(menu_item=other_item, quantity=4)
    placed_order = order_service.place_order({str(menu_item.pk): 2, str(other_item.pk): 1})
    original_add_stock = inventory_service.add_stock

    def fail_after_restoring_stock(item, quantity):
        original_add_stock(item, quantity)
        if item.pk == other_item.pk:
            raise RuntimeError('Stock restoration failed')

    with monkeypatch.context() as patch:
        patch.setattr(inventory_service, 'add_stock', fail_after_restoring_stock)
        with pytest.raises(RuntimeError, match='Stock restoration failed'):
            order_service.cancel_order(placed_order.pk, actor=staff_actor)

    placed_order.refresh_from_db()
    inventory_item.refresh_from_db()
    other_stock.refresh_from_db()
    assert placed_order.status == 'pending'
    assert inventory_item.quantity == 3
    assert other_stock.quantity == 3

    order_service.cancel_order(placed_order.pk, actor=staff_actor)
    placed_order.refresh_from_db()
    inventory_item.refresh_from_db()
    other_stock.refresh_from_db()
    assert placed_order.status == 'cancelled'
    assert inventory_item.quantity == 5
    assert other_stock.quantity == 4


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    'winner,contender',
    [('complete', 'cancel'), ('cancel', 'complete'), ('cancel', 'cancel'), ('cancel', 'add')],
)
def test_order_writes_wait_for_the_current_transition(
    placed_order, menu_item, inventory_item, winner, contender, staff_actor
):
    if connection.vendor != 'postgresql':
        pytest.skip('Concurrent order transitions require PostgreSQL')

    write_started = Event()
    operations = {
        'complete': order_service.complete_order,
        'cancel': lambda order_id: order_service.cancel_order(order_id, actor=staff_actor),
        'add': lambda order_id: order_service.add_item_to_order(order_id, menu_item.pk),
    }

    def signal_write(execute, sql, params, many, context):
        if 'FOR UPDATE' in sql or sql.startswith('UPDATE '):
            write_started.set()
        return execute(sql, params, many, context)

    def competing_write():
        close_old_connections()
        try:
            with connection.execute_wrapper(signal_write):
                operations[contender](placed_order.pk)
            return True
        except (ValueError, RuntimeError):
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            operations[winner](placed_order.pk)
            future = executor.submit(competing_write)
            assert write_started.wait(timeout=10), (
                'The competing operation did not reach the database'
            )
        assert not future.result(timeout=10)

    placed_order.refresh_from_db()
    inventory_item.refresh_from_db()
    assert placed_order.status == {'complete': 'completed', 'cancel': 'cancelled'}[winner]
    assert inventory_item.quantity == (5 if winner == 'cancel' else 3)
    assert placed_order.items.get().quantity == 2


@pytest.mark.django_db
def test_removing_an_order_item_restores_its_stock(placed_order, menu_item, inventory_item):
    order_service.remove_item_from_order(placed_order.pk, menu_item.pk, quantity=2)

    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 5
    assert not placed_order.items.exists()


@pytest.mark.django_db
def test_cancellation_rejects_customers(placed_order, inventory_item, django_user_model):
    customer = django_user_model.objects.create_user(username='customer')
    with pytest.raises(PermissionDenied):
        placed_order.cancel(actor=customer)

    placed_order.refresh_from_db()
    inventory_item.refresh_from_db()
    assert placed_order.status == 'pending'
    assert inventory_item.quantity == 3


@pytest.mark.django_db
@pytest.mark.parametrize('with_items', [True, False])
def test_order_read_interfaces_preserve_quantities_and_prices(
    menu_item, inventory_item, with_items
):
    quantity = 2 if with_items else 0
    original_price = menu_item.price
    placed_order = (
        order_service.place_order({str(menu_item.pk): quantity})
        if with_items
        else order_service.create_order()
    )
    menu_service.modify_menu_item(menu_item, price=Decimal('99.00'))
    if with_items:
        order_service.complete_order(placed_order.pk)

    lines = order_service.get_order_items(placed_order.pk)
    assert isinstance(lines, list)
    assert sum(line.quantity for line in lines) == quantity
    if with_items:
        assert isinstance(lines[0], OrderItem)
        assert lines[0].price_at_time_of_order == original_price

    total = order_service.get_order_total(placed_order.pk)
    assert isinstance(total, Decimal)
    assert total == original_price * quantity
    assert order_service.get_order(placed_order.pk).total_items == quantity
    assert order_service.get_item_quantity(placed_order.pk, menu_item.pk) == quantity

    other_item = MenuItem.objects.create(name='Not ordered', category='beverage', price='1.00')
    assert order_service.get_item_quantity(placed_order.pk, other_item.pk) == 0
