from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models.expressions import RawSQL


@pytest.mark.django_db
@pytest.mark.parametrize(
    'fixture_name,field,value',
    [
        ('menu_item', 'price', Decimal('0')),
        ('menu_item', 'category', 'unknown'),
        ('inventory_item', 'quantity', -1),
        ('inventory_item', 'low_stock_threshold', 0),
        ('order', 'status', 'unknown'),
        ('order_item', 'quantity', 0),
        ('order_item', 'price_at_time_of_order', Decimal('-1')),
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
