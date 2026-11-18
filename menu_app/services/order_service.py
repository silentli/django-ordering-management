"""
Order service for handling order-related operations.
This module manages orders and their items.
"""

import logging
from decimal import Decimal
from typing import List, Mapping, Optional

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction

from menu_app.models import order
from menu_app.services import db_utils, inventory_service, menu_utils

logger = logging.getLogger(__name__)


def _get_base_order_queryset():
    """
    Returns the base queryset for orders with items pre-selected
    to avoid repeating the select_related
    """
    return db_utils.get_model_queryset(order.Order).prefetch_related('items', 'items__menu_item')


def get_order(order_id: int) -> Optional[order.Order]:
    """
    Get an order by ID

    Raises:
        ValueError: If order_id is invalid
    """
    if not order_id:
        raise ValueError('Order ID cannot be empty')

    try:
        return _get_base_order_queryset().get(id=order_id)
    except order.Order.DoesNotExist:
        logger.warning(f'Order {order_id} not found')
        return None


def get_all_orders() -> List[order.Order]:
    """
    Get all orders with their items
    """
    return list(_get_base_order_queryset().all())


def _get_pending_order_for_update(order_id: int) -> order.Order:
    """Lock the order before checking whether a business operation is allowed."""
    try:
        order_obj = order.Order.objects.select_for_update().get(pk=order_id)
    except order.Order.DoesNotExist:
        raise ValueError(f'Order {order_id} not found') from None
    if not order_obj.is_modifiable():
        raise ValueError('Only pending orders can be modified')
    return order_obj


@transaction.atomic
def create_order() -> order.Order:
    """
    Create a new empty order
    """
    try:
        return db_utils.create_model_instance(order.Order)
    except Exception as e:
        logger.error(f'Error creating order: {e!s}')
        raise RuntimeError(f'Failed to create order: {e!s}')


@transaction.atomic
def place_order(cart: Mapping[str, int]) -> order.Order:
    """Create a whole order and deduct stock, rolling back every item on failure."""
    if not cart:
        raise ValueError('Cart cannot be empty')

    new_order = create_order()
    # Use a consistent inventory update order to avoid checkout deadlocks.
    for menu_item_id, quantity in sorted(cart.items(), key=lambda item: int(item[0])):
        add_item_to_order(new_order.id, int(menu_item_id), quantity)
    return new_order


@transaction.atomic
def add_item_to_order(order_id: int, menu_item_id: int, quantity: int = 1) -> None:
    """
    Add an item to an order and reduce inventory

    Args:
        order_id: The ID of the order
        menu_item_id: The ID of the menu item to add
        quantity: The quantity to add (default: 1)

    Raises:
        ValueError: If parameters are invalid or order/menu item not found
        RuntimeError: If there are issues adding the item
    """
    if quantity <= 0:
        raise ValueError('Quantity must be positive')

    try:
        order_obj = _get_pending_order_for_update(order_id)

        menu_item_obj = menu_utils.get_menu_item(menu_item_id=menu_item_id)
        if not menu_item_obj:
            raise ValueError(f'Menu item {menu_item_id} not found')

        # Check if item already exists in order
        existing_item = order_obj.find_order_item(menu_item_obj)
        if existing_item:
            # Check if we have enough inventory for the additional quantity
            if not menu_utils.check_item_availability(menu_item_obj, quantity):
                raise ValueError(f'Insufficient stock for {menu_item_obj.name}')
            existing_item.quantity += quantity
            db_utils.update_model_instance(existing_item, quantity=existing_item.quantity)
            # Reduce inventory for the additional quantity
            inventory_service.remove_stock(menu_item_obj, quantity)
        else:
            # Check if we have enough inventory for the new item
            if not menu_utils.check_item_availability(menu_item_obj, quantity):
                raise ValueError(f'Insufficient stock for {menu_item_obj.name}')
            # Create the order item
            db_utils.create_model_instance(
                order.OrderItem,
                order=order_obj,
                menu_item=menu_item_obj,
                quantity=quantity,
            )
            # Reduce inventory
            inventory_service.remove_stock(menu_item_obj, quantity)
    except Exception as e:
        logger.error(f'Error adding item to order {order_id}: {e!s}')
        raise RuntimeError(f'Failed to add item to order: {e!s}')


@transaction.atomic
def remove_item_from_order(order_id: int, menu_item_id: int, quantity: int = 1) -> None:
    """
    Remove an item from an order and restore inventory

    Args:
        order_id: The ID of the order
        menu_item_id: The ID of the menu item to remove
        quantity: The quantity to remove (default: 1)

    Raises:
        ValueError: If parameters are invalid or order/menu item not found
        RuntimeError: If there are issues removing the item
    """
    if quantity <= 0:
        raise ValueError('Quantity must be positive')

    try:
        order_obj = _get_pending_order_for_update(order_id)

        menu_item_obj = menu_utils.get_menu_item(menu_item_id=menu_item_id)
        if not menu_item_obj:
            raise ValueError(f'Menu item {menu_item_id} not found')

        existing_item = order_obj.find_order_item(menu_item_obj)
        if not existing_item:
            raise ValueError(f'Item {menu_item_obj.name} not found in order')

        if existing_item.quantity > quantity:
            existing_item.quantity -= quantity
            db_utils.update_model_instance(existing_item, quantity=existing_item.quantity)
            # Restore inventory for the removed quantity
            inventory_service.add_stock(menu_item_obj, quantity)
        else:
            # Restore all inventory for this item
            inventory_service.add_stock(menu_item_obj, existing_item.quantity)
            existing_item.delete()
    except Exception as e:
        logger.error(f'Error removing item from order {order_id}: {e!s}')
        raise RuntimeError(f'Failed to remove item from order: {e!s}')


def get_order_total(order_id: int) -> Decimal:
    """
    Get the total price of an order

    Args:
        order_id: The ID of the order

    Returns:
        The total price of the order

    Raises:
        ValueError: If order_id is invalid or order not found
    """
    order_obj = get_order(order_id)
    if not order_obj:
        raise ValueError(f'Order {order_id} not found')
    return order_obj.total_price


def get_order_items(order_id: int) -> List[order.OrderItem]:
    """
    Get order lines, including their quantities and prices at purchase time.

    Args:
        order_id: The ID of the order

    Returns:
        List of OrderItem objects; an empty order returns an empty list.

    Raises:
        ValueError: If order_id is invalid or order not found
    """
    order_obj = get_order(order_id)
    if not order_obj:
        raise ValueError(f'Order {order_id} not found')
    return list(order_obj.items.all())


def get_item_quantity(order_id: int, menu_item_id: int) -> int:
    """
    Get the quantity of a specific menu item in an order

    Args:
        order_id: The ID of the order
        menu_item_id: The ID of the menu item

    Returns:
        The quantity, or zero if this menu item is not part of the order.

    Raises:
        ValueError: If parameters are invalid or order/menu item not found
    """
    order_obj = get_order(order_id)
    if not order_obj:
        raise ValueError(f'Order {order_id} not found')

    menu_item_obj = menu_utils.get_menu_item(menu_item_id=menu_item_id)
    if not menu_item_obj:
        raise ValueError(f'Menu item {menu_item_id} not found')

    return next(
        (item.quantity for item in order_obj.items.all() if item.menu_item_id == menu_item_obj.pk),
        0,
    )


@transaction.atomic
def complete_order(order_id: int) -> None:
    """Complete a pending order without changing its reserved inventory."""
    order_obj = _get_pending_order_for_update(order_id)
    order_obj.status = 'completed'
    order_obj.save(update_fields=['status', 'updated_at'])


@transaction.atomic
def cancel_order(order_id: int, *, actor: User) -> None:
    """Allow active staff to cancel and restore stock in one transaction."""
    if not (actor.is_authenticated and actor.is_active and actor.is_staff):
        raise PermissionDenied('Only active staff can cancel orders.')
    order_obj = _get_pending_order_for_update(order_id)
    items = order_obj.items.select_related('menu_item').order_by('menu_item_id')
    for order_item in items:
        inventory_service.add_stock(order_item.menu_item, order_item.quantity)
    order_obj.status = 'cancelled'
    order_obj.save(update_fields=['status', 'updated_at'])
