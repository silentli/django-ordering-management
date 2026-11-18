import pytest

from menu_app.models import InventoryItem, MenuItem
from menu_app.services import inventory_service, menu_service


@pytest.mark.django_db
def test_available_menu_excludes_sold_out_and_missing_inventory(menu_item, inventory_item):
    sold_out = MenuItem.objects.create(name='Sold out', category='main', price='2.00')
    InventoryItem.objects.create(menu_item=sold_out, quantity=0)
    MenuItem.objects.create(name='No inventory', category='main', price='3.00')
    beverage = MenuItem.objects.create(name='Beverage', category='beverage', price='1.00')
    InventoryItem.objects.create(menu_item=beverage, quantity=3)

    available = menu_service.get_menu(category='MAIN', available_only=True)
    assert list(available) == [menu_item]


@pytest.mark.django_db
def test_bulk_menu_lookup_has_consistent_keys(menu_item):
    by_id = menu_service.get_menu_items(menu_item_ids=[menu_item.pk, menu_item.pk])
    by_name = menu_service.get_menu_items(names=[f' {menu_item.name} ', menu_item.name])

    assert by_id == {menu_item.pk: menu_item}
    assert by_name == {menu_item.name: menu_item}


def test_bulk_menu_lookup_requires_one_selector():
    with pytest.raises(ValueError):
        menu_service.get_menu_items()
    with pytest.raises(ValueError):
        menu_service.get_menu_items(menu_item_ids=[1], names=['Example'])


@pytest.mark.django_db
def test_menu_creation_rolls_back_if_inventory_initialization_fails(monkeypatch):
    create_inventory = inventory_service.create_inventory

    def failing_inventory(*args, **kwargs):
        create_inventory(*args, **kwargs)
        raise RuntimeError('Inventory initialization failed')

    monkeypatch.setattr(inventory_service, 'create_inventory', failing_inventory)
    with pytest.raises(RuntimeError):
        menu_service.add_menu_item(name='New item', price=2, category='main')
    assert not MenuItem.objects.exists()
    assert not InventoryItem.objects.exists()
