import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from menu_app.models import MenuItem
from menu_app.services import inventory_service, menu_service


class Command(BaseCommand):
    help = 'Explicitly load demo menu and inventory data into an empty menu'

    def add_arguments(self, parser):
        parser.add_argument(
            'data_file',
            nargs='?',
            type=Path,
            default=Path(__file__).resolve().parents[2] / 'data' / 'initial_menu_items.json',
            help='Menu JSON file (defaults to menu_app/data/initial_menu_items.json)',
        )

    def handle(self, *args, **options):
        if MenuItem.objects.exists():
            self.stdout.write(self.style.WARNING('Menu items already exist, skipping data load'))
            return

        try:
            with options['data_file'].open(encoding='utf-8') as source:
                data = json.load(source, parse_float=Decimal)
        except (OSError, ValueError) as error:
            raise CommandError(f'Unable to read demo data: {error}') from error

        try:
            quantity = data['initial_inventory_quantity']
            if type(quantity) is not int or quantity < 0:
                raise ValueError('Initial inventory quantity must be a non-negative integer')
            if not isinstance(data['menu_items'], list) or not data['menu_items']:
                raise ValueError('Menu items must be a non-empty list')

            with transaction.atomic():
                for item_data in data['menu_items']:
                    item = menu_service.add_menu_item(
                        name=item_data['name'],
                        price=Decimal(str(item_data['price'])),
                        category=item_data['category'],
                    )
                    if quantity:
                        inventory_service.add_stock(item, quantity)
        except (
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            InvalidOperation,
            IntegrityError,
        ) as error:
            raise CommandError(f'Unable to import demo data: {error}') from error

        self.stdout.write(self.style.SUCCESS('Successfully loaded initial data'))
