# -*- coding: utf-8 -*-
"""InMemoryProductRepository

开发态内存仓储实现。ProductRepository 由种子数据初始化；
订单/库存账本已撤除，目录里的 stock 是版本化快照展示值。
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.product import Product
from app.infrastructure.persistence.seed_products import build_seed_products


class InMemoryProductRepository(ProductRepository):
    def __init__(self, products: Optional[list[Product]] = None) -> None:
        seed = products if products is not None else build_seed_products()
        self._products: dict[str, Product] = {p.product_id: p for p in seed}


    async def _refresh_inventory(self, products: list[Product]) -> list[Product]:
        # 订单/库存账本已撤除：目录里的 stock 是版本化快照展示值，不再动态覆盖。
        return products

    async def find_by_id(self, product_id: str) -> Optional[Product]:
        products = await self._refresh_inventory([self._products[product_id]] if product_id in self._products else [])
        return products[0] if products else None

    async def find_by_ids(self, product_ids: list[str]) -> list[Product]:
        return await self._refresh_inventory([self._products[pid] for pid in product_ids if pid in self._products])

    async def list_all(self) -> list[Product]:
        return await self._refresh_inventory(list(self._products.values()))


