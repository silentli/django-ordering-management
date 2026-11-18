from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
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


@pytest.mark.django_db(transaction=True)
def test_snapshot_migration_preserves_existing_orders():
    previous = [('menu_app', '0002_business_data_constraints')]
    latest = [('menu_app', '0003_menu_availability_and_order_snapshots')]
    executor = MigrationExecutor(connection)
    restore_targets = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps
        item = apps.get_model('menu_app', 'MenuItem').objects.create(
            name='Existing dish', category='main', price=Decimal('15.00')
        )
        order = apps.get_model('menu_app', 'Order').objects.create(status='completed')
        line = apps.get_model('menu_app', 'OrderItem').objects.create(
            order_id=order.pk,
            menu_item_id=item.pk,
            quantity=2,
            price_at_time_of_order=Decimal('10.00'),
        )
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        new_apps = executor.loader.project_state(latest).apps
        migrated = new_apps.get_model('menu_app', 'OrderItem').objects.get(pk=line.pk)
        assert migrated.name_at_time_of_order == 'Existing dish'
        assert migrated.category_at_time_of_order == 'main'
        assert migrated.price_at_time_of_order == Decimal('10.00')
        assert migrated.quantity == 2
        assert (
            new_apps.get_model('menu_app', 'Order').objects.get(pk=order.pk).status == 'completed'
        )
        assert new_apps.get_model('menu_app', 'MenuItem').objects.get(pk=item.pk).is_active
    finally:
        MigrationExecutor(connection).migrate(restore_targets)
