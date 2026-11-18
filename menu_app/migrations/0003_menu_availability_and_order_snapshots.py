from django.db import migrations, models


def backfill_order_snapshots(apps, schema_editor):
    """Older orders can only recover the menu information available at migration time."""
    OrderItem = apps.get_model('menu_app', 'OrderItem')
    MenuItem = apps.get_model('menu_app', 'MenuItem')
    database = schema_editor.connection.alias
    menu = MenuItem.objects.using(database).filter(pk=models.OuterRef('menu_item_id'))
    OrderItem.objects.using(database).update(
        name_at_time_of_order=models.Subquery(menu.values('name')[:1]),
        category_at_time_of_order=models.Subquery(menu.values('category')[:1]),
    )


class Migration(migrations.Migration):
    dependencies = [('menu_app', '0002_business_data_constraints')]

    operations = [
        migrations.AddField(
            model_name='menuitem',
            name='is_active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='name_at_time_of_order',
            field=models.CharField(max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='category_at_time_of_order',
            field=models.CharField(max_length=100, null=True),
        ),
        migrations.RunPython(backfill_order_snapshots, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='orderitem',
            name='name_at_time_of_order',
            field=models.CharField(max_length=100),
        ),
        migrations.AlterField(
            model_name='orderitem',
            name='category_at_time_of_order',
            field=models.CharField(
                max_length=100,
                choices=[
                    ('appetizer', 'Appetizer'),
                    ('main', 'Main Course'),
                    ('dessert', 'Dessert'),
                    ('beverage', 'Beverage'),
                ],
            ),
        ),
        migrations.AlterModelOptions(
            name='orderitem',
            options={
                'ordering': ['order', 'name_at_time_of_order'],
                'verbose_name': 'Order Item',
                'verbose_name_plural': 'Order Items',
            },
        ),
    ]
