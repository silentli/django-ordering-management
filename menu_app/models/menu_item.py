from decimal import Decimal
from typing import ClassVar, List, Tuple

from django.core.validators import MinValueValidator
from django.db import models

# Matches the six-digit, two-decimal price fields and excludes numeric NaN.
MAX_ITEM_PRICE = Decimal('9999.99')


class MenuCategory(models.TextChoices):
    APPETIZER = 'appetizer', 'Appetizer'
    MAIN = 'main', 'Main Course'
    DESSERT = 'dessert', 'Dessert'
    BEVERAGE = 'beverage', 'Beverage'


class MenuItem(models.Model):
    """
    Represents a menu item with its details.
    """

    CATEGORY_CHOICES: ClassVar[List[Tuple[str, str]]] = MenuCategory.choices

    # Fields
    category = models.CharField(max_length=100, choices=CATEGORY_CHOICES)
    name = models.CharField(max_length=100, unique=True)
    price = models.DecimalField(
        max_digits=6, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))]
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering: ClassVar[List[str]] = ['category', 'name']
        constraints: ClassVar[List[models.CheckConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(price__gt=0, price__lte=MAX_ITEM_PRICE),
                name='menu_item_price_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=MenuCategory.values),
                name='menu_item_category_valid',
            ),
        ]

    def __str__(self):
        return f'{self.name} (${self.price})'

    @property
    def is_available(self):
        """Check if the menu item is available (has inventory and not sold out)."""
        return self.is_active and hasattr(self, 'inventory') and not self.inventory.is_sold_out
