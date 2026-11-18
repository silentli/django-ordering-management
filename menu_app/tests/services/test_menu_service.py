from decimal import Decimal

import pytest

from menu_app.models import InventoryItem, MenuItem
from menu_app.services import inventory_service, menu_service, order_service


@pytest.mark.django_db
def test_available_menu_excludes_sold_out_and_missing_inventory(menu_item, inventory_item):
    sold_out = MenuItem.objects.create(name='Sold out', category='main', price='2.00')
    InventoryItem.objects.create(menu_item=sold_out, quantity=0)
    MenuItem.objects.create(name='No inventory', category='main', price='3.00')
    inactive = MenuItem.objects.create(
        name='Inactive', category='main', price='2.00', is_active=False
    )
    InventoryItem.objects.create(menu_item=inactive, quantity=3)
    beverage = MenuItem.objects.create(name='Beverage', category='beverage', price='1.00')
    InventoryItem.objects.create(menu_item=beverage, quantity=3)

    available = menu_service.get_menu(category='MAIN', available_only=True)
    assert list(available) == [menu_item]


@pytest.mark.django_db
@pytest.mark.parametrize(
    'price',
    [
        Decimal('0'),
        Decimal('1.001'),
        0.1,
    ],
)
def test_menu_writes_reject_invalid_prices(menu_item, price):
    original_price = menu_item.price
    with pytest.raises(ValueError):
        menu_service.add_menu_item('Invalid price', price, 'main')
    with pytest.raises(ValueError):
        menu_service.modify_menu_item(menu_item, price=price)

    menu_item.refresh_from_db()
    assert menu_item.price == original_price
    assert MenuItem.objects.count() == 1
    assert not InventoryItem.objects.exists()


@pytest.mark.django_db
def test_menu_prices_remain_decimal():
    item = menu_service.add_menu_item('Exact price', Decimal('0.10'), 'main')
    assert isinstance(item.price, Decimal)
    item.refresh_from_db()
    assert item.price == Decimal('0.10')
    inventory_service.add_stock(item, 3)
    order = order_service.place_order({str(item.pk): 3})
    assert order_service.get_order_total(order.pk) == Decimal('0.30')
    menu_service.modify_menu_item(item, price=Decimal('9999.99'))
    item.refresh_from_db()
    assert item.price == Decimal('9999.99')
