from typing import ClassVar, List

from django.core.exceptions import ValidationError
from django.db import models

from .menu_item import MenuItem


class InventoryItem(models.Model):
    """
    Represents inventory tracking for a menu item.
    """

    # Fields
    menu_item = models.OneToOneField(MenuItem, on_delete=models.CASCADE, related_name='inventory')
    quantity = models.PositiveIntegerField(default=0)
    low_stock_threshold = models.PositiveIntegerField(default=5)

    class Meta:
        verbose_name = 'Inventory Item'
        verbose_name_plural = 'Inventory Items'
        ordering: ClassVar[List[str]] = ['menu_item__category', 'menu_item__name']
        constraints: ClassVar[List[models.CheckConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(low_stock_threshold__gt=0),
                name='inventory_threshold_positive',
            ),
        ]

    def __str__(self):
        return f'{self.menu_item.name} - {self.quantity} available'

    def clean(self):
        """Validate the inventory item."""
        if self.quantity < 0:
            raise ValidationError('Quantity cannot be negative')
        if self.low_stock_threshold <= 0:
            raise ValidationError('Low stock threshold must be positive')

    @property
    def is_low_stock(self) -> bool:
        """Check if the item is low on stock."""
        return self.quantity <= self.low_stock_threshold

    @property
    def is_sold_out(self) -> bool:
        """Check if the item is sold out."""
        return self.quantity == 0
