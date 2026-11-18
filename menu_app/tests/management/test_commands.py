import json
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from menu_app.models import InventoryItem, MenuItem


@pytest.mark.django_db
def test_demo_import_failure_rolls_back_and_retry_preserves_existing_data(tmp_path):
    source = tmp_path / 'menu.json'
    data = {
        'initial_inventory_quantity': 3,
        'menu_items': [
            {'name': 'First dish', 'price': '1.25', 'category': 'main'},
            {'name': 'Second dish', 'price': '2.50', 'category': 'invalid'},
        ],
    }
    source.write_text(json.dumps(data))

    with pytest.raises(CommandError):
        call_command('load_initial_data', source, stdout=StringIO())
    assert not MenuItem.objects.exists()
    assert not InventoryItem.objects.exists()

    data['menu_items'][1]['category'] = 'beverage'
    source.write_text(json.dumps(data))
    call_command('load_initial_data', source, stdout=StringIO())
    assert MenuItem.objects.count() == 2
    assert MenuItem.objects.get(name='First dish').price == Decimal('1.25')
    assert list(InventoryItem.objects.values_list('quantity', flat=True)) == [3, 3]

    InventoryItem.objects.filter(menu_item__name='First dish').update(quantity=1)
    call_command('load_initial_data', source, stdout=StringIO())
    assert MenuItem.objects.count() == 2
    assert InventoryItem.objects.get(menu_item__name='First dish').quantity == 1
