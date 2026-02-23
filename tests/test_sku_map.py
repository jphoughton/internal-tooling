"""Tests for Amazon ASIN-to-SKU mapping."""
import pytest
from etl.amazon_sku_map import (
    get_master_sku,
    map_amazon_sku,
    register_seller_sku,
    ASIN_TO_MASTER_SKU,
    MASTER_SKU_TO_ASIN,
    _SELLER_SKU_TO_ASIN,
)


class TestGetMasterSku:
    def test_known_asin_bulk_powder(self):
        result = get_master_sku(asin='B0CVR3BV2F')
        assert result == 'HYBP-BP0030-GFB0'

    def test_known_asin_stick_pack(self):
        result = get_master_sku(asin='B07SB6DRRM')
        assert result == 'HYPO-ST0030-BOB0'

    def test_known_asin_sleep(self):
        result = get_master_sku(asin='B08N488X3T')
        assert result == 'NSPO-ST0030-LEB0'

    def test_unknown_asin_returns_none(self):
        result = get_master_sku(asin='FAKE_ASIN_12345')
        assert result is None

    def test_known_seller_sku(self):
        # Seller SKU 'EK-8L4Z-OLVW' maps to ASIN 'B0CVR3BV2F' -> 'HYBP-BP0030-GFB0'
        result = get_master_sku(seller_sku='EK-8L4Z-OLVW')
        assert result == 'HYBP-BP0030-GFB0'

    def test_seller_sku_that_is_master_sku(self):
        # 'HYCD-ST0008-BOB0' is both a seller-SKU key and a master SKU
        result = get_master_sku(seller_sku='HYCD-ST0008-BOB0')
        assert result is not None

    def test_unknown_seller_sku_returns_none(self):
        result = get_master_sku(seller_sku='TOTALLY-UNKNOWN-SKU')
        assert result is None

    def test_asin_takes_priority_over_seller_sku(self):
        # Both provided: ASIN should win
        result = get_master_sku(
            asin='B0DP1BCX7T',
            seller_sku='EK-8L4Z-OLVW',
        )
        assert result == 'ENBP-BP0030-LEB0'  # from ASIN, not seller_sku

    def test_no_args_returns_none(self):
        result = get_master_sku()
        assert result is None


class TestMapAmazonSku:
    def test_maps_known_asin(self):
        result = map_amazon_sku('ENPO-ST0030-RSLE2', asin='B08GP68YFT')
        assert result == 'ENPO-ST0030-RSLEB0'

    def test_returns_seller_sku_for_unknown(self):
        result = map_amazon_sku('MY-SELLER-SKU', asin='FAKE_ASIN_999')
        assert result == 'MY-SELLER-SKU'

    def test_maps_by_seller_sku_alone(self):
        result = map_amazon_sku('61-1KA0-RX1O')
        assert result == 'HYPO-ST0030-VPBFLB0'

    def test_registers_mapping_as_side_effect(self):
        map_amazon_sku('NEW-TEST-SKU-999', asin='B0DP1BCX7T')
        # The seller-SKU should now be registered
        assert _SELLER_SKU_TO_ASIN.get('NEW-TEST-SKU-999') == 'B0DP1BCX7T'

    def test_none_seller_sku_returns_none(self):
        # seller_sku=None, no ASIN match -> returns None (the seller_sku)
        result = map_amazon_sku(None, asin='FAKE_ASIN')
        assert result is None


class TestRegisterSellerSku:
    def test_registers_new_mapping(self):
        register_seller_sku('BRAND-NEW-TEST-SKU', 'B0CVR3BV2F')
        assert _SELLER_SKU_TO_ASIN['BRAND-NEW-TEST-SKU'] == 'B0CVR3BV2F'
        # Verify it resolves through get_master_sku
        result = get_master_sku(seller_sku='BRAND-NEW-TEST-SKU')
        assert result == 'HYBP-BP0030-GFB0'

    def test_ignores_none_seller_sku(self):
        before = dict(_SELLER_SKU_TO_ASIN)
        register_seller_sku(None, 'B0CVR3BV2F')
        assert _SELLER_SKU_TO_ASIN == before

    def test_ignores_none_asin(self):
        register_seller_sku('SOME-SKU-123', None)
        assert 'SOME-SKU-123' not in _SELLER_SKU_TO_ASIN


class TestMappingDataIntegrity:
    def test_asin_map_has_expected_count(self):
        assert len(ASIN_TO_MASTER_SKU) == 23

    def test_reverse_map_matches(self):
        assert len(MASTER_SKU_TO_ASIN) == len(ASIN_TO_MASTER_SKU)
        for asin, master in ASIN_TO_MASTER_SKU.items():
            assert MASTER_SKU_TO_ASIN[master] == asin

    def test_all_asins_start_with_b(self):
        for asin in ASIN_TO_MASTER_SKU:
            assert asin.startswith('B'), f'ASIN {asin} does not start with B'
