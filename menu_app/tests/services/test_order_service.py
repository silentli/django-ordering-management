from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest
from django.db import close_old_connections, connection, connections, transaction

from menu_app.models import InventoryItem, MenuItem, Order, OrderItem
from menu_app.services import inventory_service, menu_service, order_service


@pytest.fixture
def placed_order(menu_item, inventory_item):
    return order_service.place_order({str(menu_item.pk): 2})


@pytest.fixture
def staff_actor(django_user_model):
    return django_user_model.objects.create_user(username='order-staff', is_staff=True)


@pytest.mark.django_db(transaction=True)
def test_competing_checkouts_cannot_buy_the_same_stock(menu_item, inventory_item):
    if connection.vendor != 'postgresql':
        pytest.skip('Concurrent checkout requires PostgreSQL')

    inventory_item.quantity = 1
    inventory_item.save()
    update_started = Event()

    def signal_update(execute, sql, params, many, context):
        if 'FOR UPDATE' in sql:
            update_started.set()
        return execute(sql, params, many, context)

    cart = {str(menu_item.id): 1}

    def checkout():
        close_old_connections()
        try:
            with connection.execute_wrapper(signal_update):
                order_service.place_order(cart)
            return True
        except ValueError:
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            order_service.place_order(cart)
            future = executor.submit(checkout)
            assert update_started.wait(timeout=10), (
                'The competing checkout did not reach its database lock'
            )
        assert not future.result(timeout=10)
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 0
    assert Order.objects.count() == 1
    assert OrderItem.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('action', ['complete', 'cancel'])
def test_terminal_order_rejects_further_transitions(
    placed_order, inventory_item, action, staff_actor
):
    if action == 'cancel':
        order_service.cancel_order(placed_order.pk, actor=staff_actor)
    else:
        order_service.complete_order(placed_order.pk)

    with pytest.raises(ValueError):
        order_service.cancel_order(placed_order.pk, actor=staff_actor)
    with pytest.raises(ValueError):
        order_service.complete_order(placed_order.pk)

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
        with pytest.raises(RuntimeError):
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
    [('complete', 'cancel'), ('cancel', 'cancel')],
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
        except ValueError:
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
def test_completed_order_preserves_purchase_snapshot(menu_item, inventory_item):
    original_name, original_category, original_price = (
        menu_item.name,
        menu_item.category,
        menu_item.price,
    )
    placed_order = order_service.place_order({str(menu_item.pk): 2})
    menu_service.modify_menu_item(
        menu_item, name='Changed menu name', category='dessert', price=Decimal('99.00')
    )
    order_service.complete_order(placed_order.pk)

    saved_order = order_service.get_order(placed_order.pk)
    line = saved_order.items.get()
    assert line.name_at_time_of_order == original_name
    assert line.category_at_time_of_order == original_category
    assert line.price_at_time_of_order == original_price
    assert saved_order.total_items == 2
    assert saved_order.total_price == original_price * 2


@pytest.mark.django_db
def test_order_item_changes_preserve_price_and_stock(placed_order, menu_item, inventory_item):
    original_price = menu_item.price
    original_name, original_category = menu_item.name, menu_item.category
    menu_service.modify_menu_item(
        menu_item, name='Renamed dish', category='dessert', price=Decimal('99.00')
    )
    order_service.add_item_to_order(placed_order.pk, menu_item.pk)
    assert placed_order.items.get().quantity == 3
    order_service.remove_item_from_order(placed_order.pk, menu_item.pk)

    with pytest.raises(ValueError):
        order_service.add_item_to_order(placed_order.pk, menu_item.pk, quantity=4)

    line = placed_order.items.get()
    inventory_item.refresh_from_db()
    assert line.quantity == 2
    assert line.price_at_time_of_order == original_price
    assert line.name_at_time_of_order == original_name
    assert line.category_at_time_of_order == original_category
    assert inventory_item.quantity == 3


@pytest.mark.django_db(transaction=True)
def test_checkout_waits_for_menu_deactivation(menu_item, inventory_item):
    if connection.vendor != 'postgresql':
        pytest.skip('Concurrent menu changes require PostgreSQL')
    checkout_started = Event()

    def signal_lock(execute, sql, params, many, context):
        if 'FOR UPDATE' in sql and MenuItem._meta.db_table in sql:
            checkout_started.set()
        return execute(sql, params, many, context)

    def checkout():
        close_old_connections()
        try:
            with connection.execute_wrapper(signal_lock):
                order_service.place_order({str(menu_item.pk): 1})
            return True
        except ValueError:
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            menu_service.modify_menu_item(menu_item, is_active=False)
            future = executor.submit(checkout)
            assert checkout_started.wait(timeout=10)
        assert not future.result(timeout=10)
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 5
    assert not Order.objects.exists()
