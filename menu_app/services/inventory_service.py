"""Inventory queries and concurrency-safe stock changes."""

from typing import Dict, List, Optional

from django.db import transaction
from django.db.models import F

from menu_app.models import InventoryItem, MenuItem


def _get_base_inventory_queryset():
    return InventoryItem.objects.select_related('menu_item')


@transaction.atomic
def create_inventory(
    menu_item: MenuItem, initial_quantity: int = 0, low_stock_threshold: int = 5
) -> InventoryItem:
    if menu_item is None or menu_item.pk is None:
        raise ValueError('Menu item must be saved before creating inventory')
    if initial_quantity < 0:
        raise ValueError('Quantity cannot be negative')
    if low_stock_threshold <= 0:
        raise ValueError('Low stock threshold must be positive')
    if InventoryItem.objects.filter(menu_item=menu_item).exists():
        raise ValueError(f'Inventory already exists for menu item {menu_item.name}')
    return InventoryItem.objects.create(
        menu_item=menu_item, quantity=initial_quantity, low_stock_threshold=low_stock_threshold
    )


def get_inventory(menu_item: MenuItem) -> Optional[InventoryItem]:
    return _get_base_inventory_queryset().filter(menu_item=menu_item).first()


def get_all_inventory_items() -> List[InventoryItem]:
    return list(_get_base_inventory_queryset())


def get_inventory_by_menu_item_name(name: str) -> Optional[InventoryItem]:
    if not name or not name.strip():
        raise ValueError('Menu item name cannot be empty')
    return _get_base_inventory_queryset().filter(menu_item__name=name.strip()).first()


def get_low_stock_items() -> List[InventoryItem]:
    """Use each item's configured threshold, including the threshold itself."""
    return list(_get_base_inventory_queryset().filter(quantity__lte=F('low_stock_threshold')))


@transaction.atomic
def add_stock(menu_item: MenuItem, quantity: int) -> None:
    if quantity <= 0:
        raise ValueError('Quantity must be positive')
    inventory_item, _ = InventoryItem.objects.get_or_create(menu_item=menu_item)
    InventoryItem.objects.filter(pk=inventory_item.pk).update(quantity=F('quantity') + quantity)


def set_stock(menu_item: MenuItem, quantity: int, expected_quantity: int) -> None:
    """Reject an absolute adjustment if the displayed stock is stale."""
    if quantity < 0 or expected_quantity < 0:
        raise ValueError('Quantity cannot be negative')
    updated = InventoryItem.objects.filter(menu_item=menu_item, quantity=expected_quantity).update(
        quantity=quantity
    )
    if not updated:
        raise ValueError('Stock has changed. Refresh the page and try again.')


@transaction.atomic
def remove_stock(menu_item: MenuItem, quantity: int) -> None:
    """Check and deduct in one statement; never spend the same stock twice."""
    if quantity <= 0:
        raise ValueError('Quantity must be positive')
    updated = InventoryItem.objects.filter(menu_item=menu_item, quantity__gte=quantity).update(
        quantity=F('quantity') - quantity
    )
    if not updated:
        raise ValueError(f'Insufficient stock for {menu_item.name}')


def check_availability(menu_item: MenuItem, quantity: int) -> bool:
    """Availability is informational; remove_stock remains the authoritative check."""
    if quantity <= 0:
        raise ValueError('Quantity must be positive')
    return InventoryItem.objects.filter(menu_item=menu_item, quantity__gte=quantity).exists()


@transaction.atomic
def update_inventory(menu_item: MenuItem, quantity_change: int) -> None:
    """Apply a signed stock delta."""
    if quantity_change == 0:
        raise ValueError('Quantity change cannot be zero')
    if quantity_change > 0:
        add_stock(menu_item, quantity_change)
    else:
        remove_stock(menu_item, -quantity_change)


@transaction.atomic
def bulk_update_inventory(updates: Dict[int, int]) -> Dict[int, bool]:
    """Report expected per-item failures; unexpected errors roll back the whole batch."""
    if not updates:
        raise ValueError('Updates dictionary cannot be empty')
    menu_items = MenuItem.objects.in_bulk(updates)
    results = {}
    for menu_item_id, quantity_change in sorted(updates.items()):
        item = menu_items.get(menu_item_id)
        if item is None:
            results[menu_item_id] = False
            continue
        try:
            update_inventory(item, quantity_change)
            results[menu_item_id] = True
        except ValueError:
            results[menu_item_id] = False
    return results
