"""Order queries and transaction-protected business operations."""

from decimal import Decimal
from typing import List, Mapping, Optional

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction

from menu_app.models import Order, OrderItem
from menu_app.services import inventory_service, menu_service


def _get_base_order_queryset():
    return Order.objects.prefetch_related('items', 'items__menu_item')


def get_order(order_id: int) -> Optional[Order]:
    if not order_id:
        raise ValueError('Order ID cannot be empty')
    return _get_base_order_queryset().filter(pk=order_id).first()


def get_all_orders() -> List[Order]:
    return list(_get_base_order_queryset())


def _get_pending_order_for_update(order_id: int) -> Order:
    """All order mutations share the same lock and state check."""
    try:
        order = Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist:
        raise ValueError(f'Order {order_id} not found') from None
    if order.status != 'pending':
        raise ValueError('Only pending orders can be modified')
    return order


def create_order() -> Order:
    return Order.objects.create()


@transaction.atomic
def place_order(cart: Mapping[str, int]) -> Order:
    """Commit the whole order and its stock deductions, or roll everything back."""
    if not cart:
        raise ValueError('Cart cannot be empty')
    order = create_order()
    # Consistent stock update ordering reduces checkout deadlocks.
    for menu_item_id, quantity in sorted(cart.items(), key=lambda item: int(item[0])):
        add_item_to_order(order.pk, int(menu_item_id), quantity)
    return order


@transaction.atomic
def add_item_to_order(order_id: int, menu_item_id: int, quantity: int = 1) -> None:
    if quantity <= 0:
        raise ValueError('Quantity must be positive')
    order = _get_pending_order_for_update(order_id)
    item = menu_service.get_menu_item(menu_item_id=menu_item_id)
    if item is None:
        raise ValueError(f'Menu item {menu_item_id} not found')

    inventory_service.remove_stock(item, quantity)
    line = order.items.filter(menu_item=item).first()
    if line is None:
        OrderItem.objects.create(
            order=order, menu_item=item, quantity=quantity, price_at_time_of_order=item.price
        )
    else:
        line.quantity += quantity
        line.save(update_fields=['quantity'])


@transaction.atomic
def remove_item_from_order(order_id: int, menu_item_id: int, quantity: int = 1) -> None:
    if quantity <= 0:
        raise ValueError('Quantity must be positive')
    order = _get_pending_order_for_update(order_id)
    item = menu_service.get_menu_item(menu_item_id=menu_item_id)
    if item is None:
        raise ValueError(f'Menu item {menu_item_id} not found')
    line = order.items.filter(menu_item=item).first()
    if line is None:
        raise ValueError(f'Item {item.name} not found in order')

    restored_quantity = min(quantity, line.quantity)
    if restored_quantity == line.quantity:
        line.delete()
    else:
        line.quantity -= restored_quantity
        line.save(update_fields=['quantity'])
    inventory_service.add_stock(item, restored_quantity)


def get_order_total(order_id: int) -> Decimal:
    order = get_order(order_id)
    if order is None:
        raise ValueError(f'Order {order_id} not found')
    return order.total_price


def get_order_items(order_id: int) -> List[OrderItem]:
    """Return order lines, including purchase quantities and prices."""
    order = get_order(order_id)
    if order is None:
        raise ValueError(f'Order {order_id} not found')
    return list(order.items.all())


def get_item_quantity(order_id: int, menu_item_id: int) -> int:
    """Return zero when an existing menu item is not part of the order."""
    order = get_order(order_id)
    if order is None:
        raise ValueError(f'Order {order_id} not found')
    item = menu_service.get_menu_item(menu_item_id=menu_item_id)
    if item is None:
        raise ValueError(f'Menu item {menu_item_id} not found')
    return next((line.quantity for line in order.items.all() if line.menu_item_id == item.pk), 0)


@transaction.atomic
def complete_order(order_id: int) -> None:
    order = _get_pending_order_for_update(order_id)
    order.status = 'completed'
    order.save(update_fields=['status', 'updated_at'])


@transaction.atomic
def cancel_order(order_id: int, *, actor: User) -> None:
    """Only active staff may cancel; stock restoration shares the order transaction."""
    if not (actor.is_authenticated and actor.is_active and actor.is_staff):
        raise PermissionDenied('Only active staff can cancel orders.')
    order = _get_pending_order_for_update(order_id)
    for line in order.items.select_related('menu_item').order_by('menu_item_id'):
        inventory_service.add_stock(line.menu_item, line.quantity)
    order.status = 'cancelled'
    order.save(update_fields=['status', 'updated_at'])
