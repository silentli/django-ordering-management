from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models.expressions import RawSQL

from menu_app.models import MenuItem, OrderItem


def assert_menu_item(menu_item, test_data):
    """Helper function to assert menu item properties."""
    assert menu_item.name == test_data['name']
    assert menu_item.category == test_data['category']
    assert menu_item.price == test_data['price']


def assert_inventory_item(inventory_item, test_data):
    """Helper function to assert inventory item properties."""
    assert inventory_item.quantity == test_data['quantity']
    if 'low_stock_threshold' in test_data:
        assert inventory_item.low_stock_threshold == test_data['low_stock_threshold']


@pytest.mark.django_db
def test_menu_item_creation(menu_item, test_data):
    assert_menu_item(menu_item, test_data)


@pytest.mark.django_db
def test_inventory_item_creation(inventory_item, test_data):
    assert_inventory_item(inventory_item, test_data)


@pytest.mark.django_db
def test_order_creation(order, menu_item):
    """Test order creation and status management."""
    assert order.status == 'pending'

    # Test order item creation
    order_item = OrderItem.objects.create(
        order=order,
        menu_item=menu_item,
        quantity=1,
        price_at_time_of_order=menu_item.price,
    )
    assert order_item.quantity == 1
    assert order_item.price_at_time_of_order == menu_item.price


@pytest.mark.django_db
@pytest.mark.parametrize(
    'fixture_name,field,value',
    [
        ('menu_item', 'price', Decimal('0')),
        ('menu_item', 'price', Decimal('NaN')),
        ('menu_item', 'category', 'unknown'),
        ('inventory_item', 'quantity', -1),
        ('inventory_item', 'low_stock_threshold', 0),
        ('order', 'status', 'unknown'),
        ('order_item', 'quantity', 0),
        ('order_item', 'price_at_time_of_order', Decimal('-1')),
        ('order_item', 'price_at_time_of_order', Decimal('NaN')),
    ],
)
def test_database_rejects_invalid_updates(request, fixture_name, field, value):
    """Bypass Python validation and field conversion to exercise database constraints."""
    instance = request.getfixturevalue(fixture_name)
    original_value = getattr(instance, field)

    with pytest.raises(IntegrityError), transaction.atomic():
        type(instance).objects.filter(pk=instance.pk).update(**{field: RawSQL('%s', [value])})

    instance.refresh_from_db()
    assert getattr(instance, field) == original_value


@pytest.mark.django_db
def test_database_rejects_invalid_bulk_inserts():
    with pytest.raises(IntegrityError), transaction.atomic():
        MenuItem.objects.bulk_create([MenuItem(name='Invalid price', category='main', price=0)])
    assert not MenuItem.objects.exists()
