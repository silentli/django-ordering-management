"""Menu queries and atomic menu management operations."""

import logging
from decimal import Decimal
from difflib import get_close_matches
from typing import Dict, List, Optional, Union

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator
from django.db import transaction
from django.db.models import Q, QuerySet

from menu_app.models import MenuItem
from menu_app.services import inventory_service

logger = logging.getLogger(__name__)


def validate_category(category: str) -> str:
    if not category or not category.strip():
        raise ValueError('Category cannot be empty')
    category = category.strip().lower()
    valid_categories = [value for value, _ in MenuItem.CATEGORY_CHOICES]
    if category not in valid_categories:
        raise ValueError(f'Invalid category. Must be one of: {", ".join(valid_categories)}')
    return category


def validate_name(name: str) -> str:
    if not name or not name.strip():
        raise ValueError('Name cannot be empty')
    return name.strip()


def validate_price(price: Decimal) -> Decimal:
    if isinstance(price, bool) or not isinstance(price, (Decimal, int)):
        raise ValueError('Price must be a Decimal amount')
    price = Decimal(price)
    try:
        DecimalValidator(max_digits=6, decimal_places=2)(price)
    except ValidationError:
        raise ValueError(
            'Price must be finite, with at most two decimal places and six digits'
        ) from None
    if price <= 0:
        raise ValueError('Price must be positive')
    return price


def get_menu_item(
    menu_item_id: Optional[int] = None, name: Optional[str] = None
) -> Optional[MenuItem]:
    """Look up exactly one ID or name; return None when it does not exist."""
    if (menu_item_id is None) == (name is None):
        raise ValueError('Provide exactly one of menu_item_id or name')
    filters = {'pk': menu_item_id} if menu_item_id is not None else {'name': name}
    return MenuItem.objects.filter(**filters).first()


def get_menu_items(
    *, menu_item_ids: Optional[List[int]] = None, names: Optional[List[str]] = None
) -> Dict[Union[int, str], MenuItem]:
    """Return existing items keyed by ID or normalized name."""
    if (menu_item_ids is None) == (names is None):
        raise ValueError('Provide exactly one of menu_item_ids or names')
    if menu_item_ids is not None:
        if not menu_item_ids:
            raise ValueError('Menu item IDs list cannot be empty')
        requested = set(menu_item_ids)
        result = MenuItem.objects.in_bulk(menu_item_ids)
    else:
        if not names:
            raise ValueError('Menu item names list cannot be empty')
        requested = {validate_name(name) for name in names}
        result = MenuItem.objects.filter(name__in=requested).in_bulk(field_name='name')
    missing = requested - result.keys()
    if missing:
        logger.warning(f'Some menu items not found: {missing}')
    return result


def get_menu(
    category: Optional[str] = None, available_only: bool = False, active_only: bool = False
) -> QuerySet[MenuItem]:
    """Return a filterable menu, optionally limited to items with stock."""
    queryset = MenuItem.objects.all()
    if category:
        queryset = queryset.filter(category=validate_category(category))
    if active_only or available_only:
        queryset = queryset.filter(is_active=True)
    if available_only:
        queryset = queryset.filter(inventory__quantity__gt=0)
    return queryset


def search_menu(query: str) -> List[MenuItem]:
    if not query or not query.strip():
        raise ValueError('Search query cannot be empty')
    query = query.strip()
    return list(MenuItem.objects.filter(Q(name__icontains=query) | Q(category__icontains=query)))


def find_menu_item_by_name(
    name: str, use_fuzzy: bool = False, cutoff: float = 0.6
) -> Optional[MenuItem]:
    """Try exact, partial, then optional fuzzy matching."""
    exact_match = get_menu_item(name=name)
    if exact_match or not use_fuzzy:
        return exact_match
    partial_match = MenuItem.objects.filter(name__icontains=name).first()
    if partial_match:
        return partial_match
    names = list(MenuItem.objects.values_list('name', flat=True))
    matches = get_close_matches(name, names, n=1, cutoff=cutoff)
    return get_menu_item(name=matches[0]) if matches else None


@transaction.atomic
def add_menu_item(name: str, price: Decimal, category: str, *, is_active: bool = True) -> MenuItem:
    name = validate_name(name)
    price = validate_price(price)
    category = validate_category(category)
    if MenuItem.objects.filter(name=name).exists():
        raise ValueError(f"Menu item with name '{name}' already exists")
    item = MenuItem.objects.create(name=name, price=price, category=category, is_active=is_active)
    inventory_service.create_inventory(item)
    return item


@transaction.atomic
def modify_menu_item(
    menu_item: MenuItem,
    name: Optional[str] = None,
    price: Optional[Decimal] = None,
    category: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> MenuItem:
    if menu_item is None:
        raise ValueError('Menu item cannot be None')
    changed_fields = []
    if name is not None:
        name = validate_name(name)
        if MenuItem.objects.filter(name=name).exclude(pk=menu_item.pk).exists():
            raise ValueError(f"Menu item with name '{name}' already exists")
        menu_item.name = name
        changed_fields.append('name')
    if price is not None:
        menu_item.price = validate_price(price)
        changed_fields.append('price')
    if category is not None:
        menu_item.category = validate_category(category)
        changed_fields.append('category')
    if is_active is not None:
        menu_item.is_active = is_active
        changed_fields.append('is_active')
    menu_item.save(update_fields=changed_fields)
    return menu_item
