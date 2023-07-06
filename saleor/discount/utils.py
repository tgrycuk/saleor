import datetime
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import (
    TYPE_CHECKING,
    Callable,
    DefaultDict,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)
from uuid import UUID

from babel.numbers import get_currency_precision
from django.conf import settings
from django.db.models import Exists, F, OuterRef, QuerySet
from django.utils import timezone
from prices import Money, TaxedMoney, fixed_discount, percentage_discount

from ..channel.models import Channel
from ..core.taxes import zero_money
from . import DiscountInfo, DiscountType, PromotionRuleInfo
from .models import (
    CheckoutLineDiscount,
    DiscountValueType,
    NotApplicable,
    Promotion,
    PromotionRule,
    Sale,
    SaleChannelListing,
    SaleTranslation,
    VoucherCustomer,
)

if TYPE_CHECKING:
    from ..account.models import User
    from ..checkout.fetch import (
        CheckoutInfo,
        CheckoutLineInfo,
        VariantPromotionRuleInfo,
    )
    from ..order.models import Order
    from ..plugins.manager import PluginsManager
    from ..product.models import (
        Collection,
        Product,
        ProductVariantChannelListing,
        ProductVariantQueryset,
        VariantChannelListingPromotionRule,
    )
    from .models import Voucher

CatalogueInfo = DefaultDict[str, Set[Union[int, str]]]
CATALOGUE_FIELDS = ["categories", "collections", "products", "variants"]


def increase_voucher_usage(voucher: "Voucher") -> None:
    """Increase voucher uses by 1."""
    voucher.used = F("used") + 1
    voucher.save(update_fields=["used"])


def decrease_voucher_usage(voucher: "Voucher") -> None:
    """Decrease voucher uses by 1."""
    voucher.used = F("used") - 1
    voucher.save(update_fields=["used"])


def add_voucher_usage_by_customer(voucher: "Voucher", customer_email: str) -> None:
    _, created = VoucherCustomer.objects.get_or_create(
        voucher=voucher, customer_email=customer_email
    )
    if not created:
        raise NotApplicable("This offer is only valid once per customer.")


def remove_voucher_usage_by_customer(voucher: "Voucher", customer_email: str) -> None:
    voucher_customer = VoucherCustomer.objects.filter(
        voucher=voucher, customer_email=customer_email
    )
    if voucher_customer:
        voucher_customer.delete()


def release_voucher_usage(voucher: Optional["Voucher"], user_email: Optional[str]):
    if not voucher:
        return
    if voucher.usage_limit:
        decrease_voucher_usage(voucher)
    if user_email:
        remove_voucher_usage_by_customer(voucher, user_email)


def get_product_discount_on_sale(
    product: "Product",
    product_collections: Set[int],
    discount: DiscountInfo,
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> Tuple[int, Callable]:
    """Return sale id, discount value if product is on sale or raise NotApplicable."""
    is_product_on_sale = (
        product.id in discount.product_ids
        or product.category_id in discount.category_ids
        or bool(product_collections.intersection(discount.collection_ids))
    )
    is_variant_on_sale = variant_id and variant_id in discount.variants_ids
    if is_product_on_sale or is_variant_on_sale:
        sale_channel_listing = discount.channel_listings.get(channel.slug)
        return discount.sale.id, discount.sale.get_discount(sale_channel_listing)
    raise NotApplicable("Discount not applicable for this product")


def get_product_discounts(
    *,
    product: "Product",
    collection_ids: Set[int],
    discounts: Iterable[DiscountInfo],
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> Iterator[Tuple[int, Callable]]:
    """Return sale ids, discount values for all discounts applicable to a product."""
    for discount in discounts:
        try:
            yield get_product_discount_on_sale(
                product, collection_ids, discount, channel, variant_id=variant_id
            )
        except NotApplicable:
            pass


def get_sale_id_with_min_price(
    *,
    product: "Product",
    price: Money,
    collection_ids: Set[int],
    discounts: Optional[Iterable[DiscountInfo]],
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> Tuple[Optional[int], Money]:
    """Return a sale_id and minimum product's price."""
    available_discounts = [
        (sale_id, discount)
        for sale_id, discount in get_product_discounts(
            product=product,
            collection_ids=collection_ids,
            discounts=discounts or [],
            channel=channel,
            variant_id=variant_id,
        )
    ]
    if not available_discounts:
        return None, price

    applied_discount = min(
        [(sale_id, discount(price)) for sale_id, discount in available_discounts],
        key=lambda d: d[1],  # sort over a min price
    )
    return applied_discount


def calculate_discounted_price(
    *,
    product: "Product",
    price: Money,
    collection_ids: Set[int],
    discounts: Optional[Iterable[DiscountInfo]],
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> Money:
    """Return minimum product's price of all prices with discounts applied."""
    if discounts:
        _, price = get_sale_id_with_min_price(
            product=product,
            price=price,
            collection_ids=collection_ids,
            discounts=discounts,
            channel=channel,
            variant_id=variant_id,
        )
    return price


def calculate_discounted_price_for_rules(
    *, price: Money, rules: Iterable["PromotionRule"], currency: str
):
    """Calculate the discounted price for provided rules.

    The discounts from rules summed up and applied to the price.
    """
    total_discount = zero_money(currency)
    for rule in rules:
        discount = rule.get_discount(currency)
        total_discount += price - discount(price)

    return max(price - total_discount, zero_money(currency))


def get_sale_id_applied_as_a_discount(
    *,
    product: "Product",
    price: Money,
    collections: Iterable["Collection"],
    discounts: Optional[Iterable[DiscountInfo]],
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> Optional[int]:
    """Return an ID of Sale applied to product."""
    if not discounts:
        return None

    collection_ids = {collection.id for collection in collections}
    sale_id, _ = get_sale_id_with_min_price(
        product=product,
        price=price,
        collection_ids=collection_ids,
        discounts=discounts,
        channel=channel,
        variant_id=variant_id,
    )
    return sale_id


def calculate_discounted_price_for_promotions(
    *,
    price: Money,
    rules_info_per_promotion_id: Dict[int, List[PromotionRuleInfo]],
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> List[Tuple[int, Money]]:
    """Return minimum product's price of all prices with promotions applied."""
    applied_discounts = []
    if rules_info_per_promotion_id:
        applied_discounts = get_best_promotion_discount(
            price, rules_info_per_promotion_id, channel, variant_id
        )
    return applied_discounts


def get_best_promotion_discount(
    price: Money,
    rules_info_per_promotion_id: Dict[int, List[PromotionRuleInfo]],
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> List[Tuple[int, Money]]:
    """Return the rules with the discount amounts for the best promotion.

    The data for the promotion that gives the best saving are returned in the following
    shape:
    [
        (rule_id_1, discount_amount_1),
        (rule_id_2, discount_amount_2),
    ]
    """
    available_discounts = [
        [
            (rule_id, discount)
            for rule_id, discount in get_product_promotion_discounts(
                rules_info=rules_info,
                channel=channel,
                variant_id=variant_id,
            )
        ]
        for _, rules_info in rules_info_per_promotion_id.items()
    ]
    applied_discounts = max(
        [
            [(rule_id, price - discount(price)) for rule_id, discount in discounts]
            for discounts in available_discounts
        ],
        key=lambda x: sum(y[1].amount for y in x),  # sort over a max discount
    )

    return applied_discounts


def get_product_promotion_discounts(
    *,
    rules_info: List[PromotionRuleInfo],
    channel: "Channel",
    variant_id: Optional[int],
) -> Iterator[Tuple[int, Callable]]:
    """Return promotion rule id, discount value for all rules applicable on product."""
    for rule_info in rules_info:
        try:
            yield get_product_discount_on_promotion(rule_info, channel, variant_id)
        except NotApplicable:
            pass


def get_product_discount_on_promotion(
    rule_info: PromotionRuleInfo,
    channel: "Channel",
    variant_id: Optional[int] = None,
) -> Tuple[int, Callable]:
    """Return rule id, discount value if rule applied or raise NotApplicable."""
    if variant_id in rule_info.variant_ids and channel.id in rule_info.channel_ids:
        return rule_info.rule.id, rule_info.rule.get_discount(channel.currency_code)
    raise NotApplicable("Promotion rule not applicable for this product")


def validate_voucher_for_checkout(
    manager: "PluginsManager",
    voucher: "Voucher",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
):
    from ..checkout import base_calculations
    from ..checkout.utils import calculate_checkout_quantity

    quantity = calculate_checkout_quantity(lines)
    subtotal = base_calculations.base_checkout_subtotal(
        lines,
        checkout_info.channel,
        checkout_info.checkout.currency,
    )

    customer_email = cast(str, checkout_info.get_customer_email())
    validate_voucher(
        voucher,
        subtotal,
        quantity,
        customer_email,
        checkout_info.channel,
        checkout_info.user,
    )


def validate_voucher_in_order(order: "Order"):
    subtotal = order.get_subtotal()
    quantity = order.get_total_quantity()
    customer_email = order.get_customer_email()
    if not order.voucher:
        return

    tax_configuration = order.channel.tax_configuration
    prices_entered_with_tax = tax_configuration.prices_entered_with_tax

    value = subtotal.gross if prices_entered_with_tax else subtotal.net
    validate_voucher(
        order.voucher, value, quantity, customer_email, order.channel, order.user
    )


def validate_voucher(
    voucher: "Voucher",
    total_price: TaxedMoney,
    quantity: int,
    customer_email: str,
    channel: Channel,
    customer: Optional["User"],
) -> None:
    voucher.validate_min_spent(total_price, channel)
    voucher.validate_min_checkout_items_quantity(quantity)
    if voucher.apply_once_per_customer:
        voucher.validate_once_per_customer(customer_email)
    if voucher.only_for_staff:
        voucher.validate_only_for_staff(customer)


def get_products_voucher_discount(
    voucher: "Voucher", prices: Iterable[Money], channel: Channel
) -> Money:
    """Calculate discount value for a voucher of product or category type."""
    if voucher.apply_once_per_order:
        return voucher.get_discount_amount_for(min(prices), channel)
    discounts = (voucher.get_discount_amount_for(price, channel) for price in prices)
    total_amount = sum(discounts, zero_money(channel.currency_code))
    return total_amount


def fetch_categories(
    sale_pks: Iterable[str],
    lines_info: Iterable["CheckoutLineInfo"] = [],
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> Dict[int, Set[int]]:
    from ..product.models import Category

    categories = (
        Sale.categories.through.objects.using(database_connection_name)
        .filter(sale_id__in=sale_pks)
        .order_by("id")
        .values_list("sale_id", "category_id")
    )
    category_map: Dict[int, Set[int]] = defaultdict(set)
    for sale_pk, category_pk in categories:
        category_map[sale_pk].add(category_pk)

    used_category_pks = {line_info.product.category_id for line_info in lines_info}

    subcategory_map: Dict[int, Set[int]] = defaultdict(set)
    for sale_pk, category_pks in category_map.items():
        subcategories = Category.tree.filter(pk__in=category_pks).get_descendants(
            include_self=True
        )

        if used_category_pks:
            subcategories = subcategories.filter(pk__in=used_category_pks)

        subcategory_map[sale_pk] = set(subcategories.values_list("pk", flat=True))
    return subcategory_map


def fetch_collections(
    sale_pks: Iterable[str],
    lines_info: Iterable["CheckoutLineInfo"] = [],
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> Dict[int, Set[int]]:
    collections = Sale.collections.through.objects.using(
        database_connection_name
    ).filter(sale_id__in=sale_pks)

    if lines_info:
        collection_pks = [
            collection.pk
            for line_info in lines_info
            for collection in line_info.collections
        ]
        collections = collections.filter(collection_id__in=collection_pks)

    collection_map: Dict[int, Set[int]] = defaultdict(set)
    for sale_pk, collection_pk in collections.order_by("id").values_list(
        "sale_id", "collection_id"
    ):
        collection_map[sale_pk].add(collection_pk)
    return collection_map


def fetch_products(
    sale_pks: Iterable[str],
    lines_info: Iterable["CheckoutLineInfo"] = [],
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> Dict[int, Set[int]]:
    product_qs = Sale.products.through.objects.using(database_connection_name).filter(
        sale_id__in=sale_pks
    )

    if lines_info:
        product_pks = [line_info.product.pk for line_info in lines_info]
        product_qs = product_qs.filter(product_id__in=product_pks)

    product_map: Dict[int, Set[int]] = defaultdict(set)
    for sale_pk, product_pk in product_qs.order_by("id").values_list(
        "sale_id", "product_id"
    ):
        product_map[sale_pk].add(product_pk)
    return product_map


def fetch_variants(
    sale_pks: Iterable[str],
    lines_info: Iterable["CheckoutLineInfo"] = [],
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> Dict[int, Set[int]]:
    variant_qs = Sale.variants.through.objects.using(database_connection_name).filter(
        sale_id__in=sale_pks
    )

    if lines_info:
        variant_pks = [line_info.variant.pk for line_info in lines_info]
        variant_qs = variant_qs.filter(productvariant_id__in=variant_pks)

    variants_map: Dict[int, Set[int]] = defaultdict(set)
    for sale_pk, variant_pk in variant_qs.order_by("id").values_list(
        "sale_id", "productvariant_id"
    ):
        variants_map[sale_pk].add(variant_pk)
    return variants_map


def fetch_sale_channel_listings(
    sale_pks: Iterable[str],
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
):
    channel_listings = (
        SaleChannelListing.objects.using(database_connection_name)
        .filter(sale_id__in=sale_pks)
        .annotate(channel_slug=F("channel__slug"))
    )
    channel_listings_map: Dict[int, Dict[str, SaleChannelListing]] = defaultdict(dict)
    for channel_listing in channel_listings:
        sale_id_row = channel_listings_map[channel_listing.sale_id]
        sale_id_row[channel_listing.channel_slug] = channel_listing
    return channel_listings_map


def fetch_discounts(date: datetime.date) -> List[DiscountInfo]:
    sales = list(Sale.objects.active(date))
    pks = {s.pk for s in sales}
    collections = fetch_collections(pks)
    channel_listings = fetch_sale_channel_listings(pks)
    products = fetch_products(pks)
    categories = fetch_categories(pks)
    variants = fetch_variants(pks)

    return [
        DiscountInfo(
            sale=sale,
            category_ids=categories[sale.pk],
            channel_listings=channel_listings[sale.pk],
            collection_ids=collections[sale.pk],
            product_ids=products[sale.pk],
            variants_ids=variants[sale.pk],
        )
        for sale in sales
    ]


def fetch_active_discounts() -> List[DiscountInfo]:
    return fetch_discounts(timezone.now())


def fetch_catalogue_info(instance: Sale) -> CatalogueInfo:
    catalogue_info: CatalogueInfo = defaultdict(set)
    for sale_data in Sale.objects.filter(id=instance.id).values(*CATALOGUE_FIELDS):
        for field in CATALOGUE_FIELDS:
            if id := sale_data.get(field):
                catalogue_info[field].add(id)

    return catalogue_info


def apply_discount_to_value(
    value: Decimal,
    value_type: str,
    currency: str,
    price_to_discount: Union[Money, TaxedMoney],
):
    """Calculate the price based on the provided values."""
    if value_type == DiscountValueType.FIXED:
        discount_method = fixed_discount
        discount_kwargs = {"discount": Money(value, currency)}
    else:
        discount_method = percentage_discount
        discount_kwargs = {"percentage": value, "rounding": ROUND_HALF_UP}
    discount = partial(
        discount_method,
        **discount_kwargs,
    )
    return discount(price_to_discount)


def fetch_active_sales_for_checkout(
    lines_info: Iterable["CheckoutLineInfo"],
) -> Iterable[DiscountInfo]:
    """Return list of `DiscountInfo` applicable for list of `CheckoutLineInfo`.

    Return list of `DiscountInfo` applicable for list of `CheckoutLineInfo`.
    This function returns only a list of `DiscountInfo` for `Sale` which
    are able to be applied on specified lines. When the function receives empty
    lines in input we return an empty list of applicable `DiscountInfo`.
    """
    if not lines_info:
        return []

    sales = list(Sale.objects.active(timezone.now()))

    pks = {s.pk for s in sales}
    channel_listings = fetch_sale_channel_listings(pks)
    product_pks_by_sale_pk_map = fetch_products(pks, lines_info)
    variant_pks_by_sale_pk_map = fetch_variants(pks, lines_info)
    category_pks_by_sale_pk_map = fetch_categories(pks, lines_info)
    collection_pks_by_sale_pk_map = fetch_collections(pks, lines_info)

    discounts_info = []
    for sale in sales:
        category_ids = category_pks_by_sale_pk_map[sale.pk]
        collection_ids = collection_pks_by_sale_pk_map[sale.pk]
        product_ids = product_pks_by_sale_pk_map[sale.pk]
        variants_ids = variant_pks_by_sale_pk_map[sale.pk]
        if category_ids or collection_ids or product_ids or variants_ids:
            discounts_info.append(
                DiscountInfo(
                    sale=sale,
                    category_ids=category_ids,
                    channel_listings=channel_listings[sale.pk],
                    collection_ids=collection_ids,
                    product_ids=product_ids,
                    variants_ids=variants_ids,
                )
            )

    return discounts_info


def is_sale_applicable_on_line(
    line_info: "CheckoutLineInfo",
    discount: DiscountInfo,
) -> bool:
    collection_ids = set(collection.id for collection in line_info.collections)
    is_product_on_sale = line_info.product.id in discount.product_ids
    is_variant_on_sale = line_info.variant.id in discount.variants_ids
    is_category_on_sale = line_info.product.category_id in discount.category_ids
    is_collection_on_sale = bool(collection_ids.intersection(discount.collection_ids))
    return (
        is_product_on_sale
        or is_variant_on_sale
        or is_category_on_sale
        or is_collection_on_sale
    )


def _apply_percentage_sale_on_lines(
    lines_info: Iterable["CheckoutLineInfo"],
    sale_info: DiscountInfo,
    sales_data_by_line_map: Dict[UUID, dict],
    currency_precision: Decimal,
):
    """Calculate discount amounts for percentage sales.

    This function calculates the discount amount for the sale. If the calculated
    discount amount is greater than the currently applied discount, the function
    saves the sale as the best sale for the calculated lines. All data are stored
    in the `sales_data_by_line_map`.
    """
    sale = sale_info.sale
    qualified_lines = []
    base_line_total_price_by_line_id_map = defaultdict(Decimal)
    remaining_total_amount = Decimal(0)
    for line_info in lines_info:
        if is_sale_applicable_on_line(line_info, sale_info):
            qualified_lines.append(line_info)
            line = line_info.line
            quantity = line.quantity
            base_unit_price = line_info.variant.get_base_price(
                line_info.channel_listing, line.price_override
            )
            base_unit_price = cast(Money, base_unit_price)
            base_line_total_price = base_unit_price * quantity
            base_line_total_amount = base_line_total_price.amount

            remaining_total_amount += base_line_total_amount
            base_line_total_price_by_line_id_map[line.id] = base_line_total_amount

    sale_channel_listing = sale_info.channel_listings.get(line_info.channel.slug)
    if sale_channel_listing and remaining_total_amount != Decimal(0):
        currency = sale_channel_listing.currency
        discounted_amount = apply_discount_to_value(
            sale_channel_listing.discount_value,
            sale.type,
            currency,
            Money(remaining_total_amount, currency),
        ).amount
        remaining_discount_amount = remaining_total_amount - discounted_amount
        for line_info in qualified_lines:
            line = line_info.line
            base_line_total_amount = base_line_total_price_by_line_id_map[line.pk]
            line_discount_amount = Decimal(
                base_line_total_amount
                * remaining_discount_amount
                / remaining_total_amount
            ).quantize(currency_precision, ROUND_HALF_UP)
            remaining_discount_amount -= line_discount_amount
            remaining_total_amount -= base_line_total_amount
            if (
                sales_data_by_line_map[line.id].get(
                    "best_discount_amount",
                    Decimal("-Inf"),
                )
                < line_discount_amount
            ):
                sales_data_by_line_map[line.id] = {
                    "sale": sale,
                    "sale_channel_listing": sale_channel_listing,
                    "best_discount_amount": line_discount_amount,
                }


def _apply_fixed_sale_on_lines(
    lines_info: Iterable["CheckoutLineInfo"],
    sale_info: DiscountInfo,
    sales_data_by_line_map: Dict[UUID, dict],
):
    """Calculate discount amounts for for fixed sales.

    This function calculates the discount amount for the sale. If the calculated
    discount amount is greater than the currently applied discount, the function
    saves the sale as the best sale for the calculated lines. All data are stored
    in the `sales_data_by_line_map`.
    """
    sale = sale_info.sale
    for line_info in lines_info:
        if is_sale_applicable_on_line(line_info, sale_info):
            line = line_info.line
            quantity = line.quantity
            base_unit_price = line_info.variant.get_base_price(
                line_info.channel_listing, line.price_override
            )
            base_unit_price = cast(Money, base_unit_price)

            channel_listing = sale_info.channel_listings.get(line_info.channel.slug)
            if not channel_listing:
                continue
            unit_price_with_applied_sale = apply_discount_to_value(
                channel_listing.discount_value,
                sale.type,
                channel_listing.currency,
                base_unit_price,
            )
            unit_price_with_applied_sale = cast(Money, unit_price_with_applied_sale)
            unit_discount = min(
                base_unit_price - unit_price_with_applied_sale,
                base_unit_price,
            )
            total_discount = unit_discount * quantity
            discount_amount = total_discount.amount
            if (
                sales_data_by_line_map[line.id].get(
                    "best_discount_amount", Decimal("-Inf")
                )
                < discount_amount
            ):
                sales_data_by_line_map[line.id] = {
                    "sale": sale,
                    "sale_channel_listing": channel_listing,
                    "best_discount_amount": discount_amount,
                }


def create_or_update_discount_objects_from_sale_for_checkout(
    checkout_info: "CheckoutInfo",
    lines_info: Iterable["CheckoutLineInfo"],
    sales_info: Iterable[DiscountInfo],
):
    line_discounts_to_create = []
    line_discounts_to_update = []
    updated_fields = []

    currency_precision = Decimal("0.1") ** get_currency_precision(
        checkout_info.checkout.currency
    )

    sales_data_by_line_map: Dict[UUID, dict] = defaultdict(dict)

    for sale_info in sales_info:
        if sale_info.sale.type == DiscountValueType.FIXED:
            _apply_fixed_sale_on_lines(lines_info, sale_info, sales_data_by_line_map)

        else:
            _apply_percentage_sale_on_lines(
                lines_info, sale_info, sales_data_by_line_map, currency_precision
            )

    for line_info in lines_info:
        line = line_info.line
        sale = sales_data_by_line_map[line.id].get("sale")
        sale_channel_listing = sales_data_by_line_map[line.id].get(
            "sale_channel_listing"
        )
        discount_amount = sales_data_by_line_map[line.id].get("best_discount_amount")

        if sale and sale_channel_listing and discount_amount:
            sale = cast(Sale, sale)
            sale_channel_listing = cast(SaleChannelListing, sale_channel_listing)

            # Fetch Sale translation
            translation_language_code = checkout_info.checkout.language_code
            sale_translation = SaleTranslation.objects.filter(
                sale_id=sale.pk, language_code=translation_language_code
            ).first()
            translated_name = None
            if sale_translation:
                translated_name = sale_translation.name
            discount_to_update = line_info.get_sale_discount()
            if not discount_to_update:
                line_discount = CheckoutLineDiscount(
                    line=line,
                    type=DiscountType.SALE,
                    value_type=sale.type,
                    value=sale_channel_listing.discount_value,
                    amount_value=discount_amount,
                    currency=line.currency,
                    name=sale.name,
                    translated_name=translated_name,
                    reason=None,
                    sale=sale,
                )
                line_discounts_to_create.append(line_discount)
                line_info.discounts.append(line_discount)
            else:
                if discount_to_update.value_type != sale.type:
                    discount_to_update.value_type = sale.type
                    updated_fields.append("value_type")
                if discount_to_update.value != sale_channel_listing.discount_value:
                    discount_to_update.value = sale_channel_listing.discount_value
                    updated_fields.append("value")
                if discount_to_update.amount_value != discount_amount:
                    discount_to_update.amount_value = discount_amount
                    updated_fields.append("amount_value")
                if discount_to_update.name != sale.name:
                    discount_to_update.name = sale.name
                    updated_fields.append("name")
                if discount_to_update.translated_name != translated_name:
                    discount_to_update.translated_name = translated_name
                    updated_fields.append("translated_name")
                if discount_to_update.sale != sale:
                    discount_to_update.sale = sale
                    updated_fields.append("sale")

                line_discounts_to_update.append(discount_to_update)

    if line_discounts_to_create:
        CheckoutLineDiscount.objects.bulk_create(line_discounts_to_create)
    if line_discounts_to_update and updated_fields:
        CheckoutLineDiscount.objects.bulk_update(
            line_discounts_to_update, updated_fields
        )


def create_or_update_discount_objects_from_promotion_for_checkout(
    lines_info: Iterable["CheckoutLineInfo"],
):
    line_discounts_to_create = []
    line_discounts_to_update = []
    line_discount_ids_to_remove = []
    updated_fields: List[str] = []

    for line_info in lines_info:
        line = line_info.line

        # discount_amount based on the difference between discounted_price and price
        discount_amount = _get_discount_amount(line_info.channel_listing, line.quantity)

        # get the existing discounts for the line
        discounts_to_update = line_info.get_promotion_discounts()
        rule_id_to_discount = {
            discount.promotion_rule_id: discount for discount in discounts_to_update
        }

        # delete all existing discounts if the line is not discounted
        if not discount_amount:
            ids_to_remove = [discount.id for discount in discounts_to_update]
            CheckoutLineDiscount.objects.filter(id__in=ids_to_remove).delete()
            line_info.discounts = []
            continue

        # delete the discount objects that are not valid anymore
        line_discount_ids_to_remove.extend(
            _get_discounts_that_are_not_valid_anymore(
                line_info.rules_info,
                rule_id_to_discount,  # type: ignore[arg-type]
                line_info,
            )
        )

        for rule_info in line_info.rules_info:
            rule = rule_info.rule
            discount_to_update = rule_id_to_discount.get(rule.id)
            rule_discount_amount = _get_rule_discount_amount(
                rule_info.variant_listing_promotion_rule, line.quantity
            )
            discount_name = get_discount_name(rule, rule_info.promotion)
            if not discount_to_update:
                line_discount = CheckoutLineDiscount(
                    line=line,
                    type=DiscountType.PROMOTION,
                    value_type=rule.reward_value_type,
                    value=rule.reward_value,
                    amount_value=rule_discount_amount,
                    currency=line.currency,
                    name=discount_name,
                    # TODO: set the promotion translation
                    translated_name="",
                    reason=None,
                    promotion_rule=rule,
                )
                line_discounts_to_create.append(line_discount)
                line_info.discounts.append(line_discount)
            else:
                _update_line_discount(
                    rule,
                    rule_info.promotion,
                    rule_discount_amount,
                    discount_to_update,
                    updated_fields,
                )

                line_discounts_to_update.append(discount_to_update)

    if line_discounts_to_create:
        CheckoutLineDiscount.objects.bulk_create(line_discounts_to_create)
    if line_discounts_to_update and updated_fields:
        CheckoutLineDiscount.objects.bulk_update(
            line_discounts_to_update, updated_fields
        )
    if line_discount_ids_to_remove:
        CheckoutLineDiscount.objects.filter(id__in=line_discount_ids_to_remove).delete()


def _get_discount_amount(
    variant_channel_listing: "ProductVariantChannelListing", line_quantity: int
) -> Decimal:
    price_amount = variant_channel_listing.price_amount
    discounted_price_amount = variant_channel_listing.discounted_price_amount

    if (
        price_amount is None
        or discounted_price_amount is None
        or price_amount == discounted_price_amount
    ):
        return Decimal("0.0")

    unit_discount = price_amount - discounted_price_amount
    return unit_discount * line_quantity


def _get_discounts_that_are_not_valid_anymore(
    rules_info: List["VariantPromotionRuleInfo"],
    rule_id_to_discount: Dict[int, "CheckoutLineDiscount"],
    line_info: "CheckoutLineInfo",
):
    discount_ids = []
    rule_ids = {rule_info.rule.id for rule_info in rules_info}
    for rule_id, discount in rule_id_to_discount.items():
        if rule_id not in rule_ids:
            discount_ids.append(discount.id)
            line_info.discounts.remove(discount)
    return discount_ids


def _get_rule_discount_amount(
    variant_listing_promotion_rule: "VariantChannelListingPromotionRule",
    line_quantity: int,
) -> Decimal:
    discount_amount = variant_listing_promotion_rule.discount_amount
    return discount_amount * line_quantity


def get_discount_name(rule: "PromotionRule", promotion: "Promotion"):
    if promotion.name and rule.name:
        return f"{promotion.name}: {rule.name}"
    return rule.name or promotion.name


def _update_line_discount(
    rule: "PromotionRule",
    promotion: "Promotion",
    rule_discount_amount: Decimal,
    discount_to_update: "CheckoutLineDiscount",
    updated_fields: List[str],
):
    if discount_to_update.value_type != rule.reward_value_type:
        discount_to_update.value_type = (
            rule.reward_value_type  # type: ignore[assignment]
        )
        updated_fields.append("value_type")
    if discount_to_update.value != rule.reward_value:
        discount_to_update.value = rule.reward_value  # type: ignore[assignment]
        updated_fields.append("value")
    if discount_to_update.amount_value != rule_discount_amount:
        discount_to_update.amount_value = rule_discount_amount
        updated_fields.append("amount_value")
    discount_name = get_discount_name(rule, promotion)
    if discount_to_update.name != discount_name:
        discount_to_update.name = discount_name
        updated_fields.append("name")
    # TODO: after adding a transaltion update the translated_name


def generate_sale_discount_objects_for_checkout(
    checkout_info: "CheckoutInfo",
    lines_info: Iterable["CheckoutLineInfo"],
):
    sales_info = fetch_active_sales_for_checkout(lines_info)
    create_or_update_discount_objects_from_sale_for_checkout(
        checkout_info, lines_info, sales_info
    )


def fetch_active_promotion_rules(
    variant_qs: "ProductVariantQueryset",
    date: Optional[datetime.date] = None,
) -> Dict[int, List[PromotionRuleInfo]]:
    from ..graphql.discount.utils import get_variants_for_predicate

    rules_info_per_promotion_id = defaultdict(list)

    promotions = Promotion.objects.active(date)
    rules = PromotionRule.objects.filter(
        Exists(promotions.filter(id=OuterRef("promotion_id")))
    ).prefetch_related("channels")
    rule_to_channel_ids_map = _get_rule_to_channel_ids_map(rules)
    for rule in rules.iterator():
        variants = get_variants_for_predicate(rule.catalogue_predicate, variant_qs)
        rules_info_per_promotion_id[rule.promotion_id].append(
            PromotionRuleInfo(
                rule=rule,
                variants=variants,
                variant_ids=list(variants.values_list("id", flat=True)),
                channel_ids=rule_to_channel_ids_map.get(rule.id, []),
            )
        )
    return rules_info_per_promotion_id


def _get_rule_to_channel_ids_map(rules: QuerySet):
    rule_to_channel_ids_map = defaultdict(list)
    PromotionRuleChannel = PromotionRule.channels.through
    promotion_rule_channels = PromotionRuleChannel.objects.filter(
        Exists(rules.filter(id=OuterRef("promotionrule_id")))
    )
    for promotion_rule_channel in promotion_rule_channels:
        rule_id = promotion_rule_channel.promotionrule_id  # type: ignore[attr-defined]
        channel_id = promotion_rule_channel.channel_id  # type: ignore[attr-defined]
        rule_to_channel_ids_map[rule_id].append(channel_id)
    return rule_to_channel_ids_map
