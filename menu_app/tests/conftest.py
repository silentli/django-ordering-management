from decimal import Decimal

import pytest

from menu_app.models.inventory import InventoryItem
from menu_app.models.menu_item import MenuItem
from menu_app.models.order import Order


@pytest.fixture
def test_data():
    """Fixture providing test data for menu items."""
    return {
        'name': 'Test Item',
        'category': 'main',
        'price': Decimal('10.99'),
        'quantity': 5,
    }


@pytest.fixture
def menu_item(test_data):
    """Fixture providing a test menu item."""
    return MenuItem.objects.create(
        name=test_data['name'], category=test_data['category'], price=test_data['price']
    )


@pytest.fixture
def inventory_item(menu_item, test_data):
    """Fixture providing a test inventory item."""
    return InventoryItem.objects.create(menu_item=menu_item, quantity=test_data['quantity'])


@pytest.fixture
def order(menu_item):
    """Fixture providing a test order."""
    return Order.objects.create(status='pending')


@pytest.fixture
def order_item(order, menu_item):
    """Create a test order item."""
    return order.items.create(
        menu_item=menu_item,
        quantity=1,
        price_at_time_of_order=menu_item.price,
        name_at_time_of_order=menu_item.name,
        category_at_time_of_order=menu_item.category,
    )
