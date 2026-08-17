import unittest

from analyzer import (
    DOC_BUILDING,
    DOC_COLLECTIVE,
    DOC_LAND,
    DeedDocument,
    analyze_property,
    extract_mortgages,
    has_active_separate_land_registry,
    merge_mortgages,
    parse_deed_document,
)


class SeparateLandRegistryTest(unittest.TestCase):
    def test_requires_land_registry_when_separate_registry_is_active(self):
        pyojebu = """
        ( 대지권의 표시 )
        1 별도등기 있음 1토지(을구 13번 근저당권설정등기)
        """

        self.assertTrue(has_active_separate_land_registry(pyojebu))


class MortgageExtractionTest(unittest.TestCase):
    def test_cancels_every_reference_when_a_cancellation_purpose_wraps_to_the_next_line(self):
        eulgu = """
        1 근저당권설정 2013년1월21일 채권최고액 금282,720,000원
        2 근저당권설정 2013년1월21일 채권최고액 금559,200,000원
        3 근저당권설정 2013년2월8일 채권최고액 금58,500,000원
        4 1번근저당권설정,
        2번근저당권설정 제3866호 해지
        등기말소
        5 근저당권설정 2024년8월16일 채권최고액 금54,000,000원
        6 3번근저당권설정등
        기말소 제133907호 해지
        """

        total, items = extract_mortgages(eulgu)

        self.assertEqual(total, 54_000_000)
        self.assertEqual(
            {item["rank"]: item["status"] for item in items},
            {5: "active"},
        )

    def test_uses_latest_mortgage_change_and_ignores_footer_notice(self):
        eulgu = """
        1 근저당권설정 2019년10월22일 채권최고액 금884,000,000원
        근저당권자 양주신용협동조합
        1-1 1번근저당권변경 2020년3월10일 채권최고액 금72,000,000원
        * 실선으로 그어진 부분은 말소사항을 표시함.
        """

        total, items = extract_mortgages(eulgu)

        self.assertEqual(total, 72_000_000)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["amount"], 72_000_000)
        self.assertEqual(items[0]["status"], "active")


class StructuredOwnerRowsTest(unittest.TestCase):
    def test_uses_structured_rows_for_wrapped_partial_share_transfers(self):
        raw_text = """
        【 갑 구 】 ( 소유권에 관한 사항 )
        텍스트 추출이 표 셀 순서를 섞어 놓은 갑구 원문
        [[OWNER_ROW]]1|소유권이전|공유자 지분 2분의 1 김만대 540504-******* 지분 2분의 1 이순동 601126-*******
        [[OWNER_ROW]]2|1번김만대지분2분의 1 중 일부(4분의1), 1번이순동지분2분의 1 중 일부(4분의1)이전|공유자 지분 2분의 1 지상붕 570926-*******
        [[OWNER_ROW]]3|1번김만대지분전부, 1번이순동지분전부 이전|공유자 지분 2분의 1 김현순 610726-*******
        [[OWNER_ROW]]4|3번김현순지분전부 이전|공유자 지분 2분의 1 지안나 850521-*******
        [[OWNER_ROW]]5|4번근저당권이전|근저당권자 하나은행 111111-*******
        【 을 구 】 ( 소유권 이외의 권리에 관한 사항 )
        """

        document = parse_deed_document(raw_text)

        self.assertEqual(document.current_owner, "지상붕, 지안나")
        self.assertEqual(
            document.owner_names,
            ["김만대", "이순동", "지상붕", "김현순", "지안나"],
        )

    def test_analysis_returns_only_current_owners_not_owner_history(self):
        document = DeedDocument(
            doc_type=DOC_COLLECTIVE,
            current_owner="지상붕, 지안나",
            owner_names=["김만대", "이순동", "지상붕", "김현순", "지안나"],
            has_daejigwon=True,
        )

        result = analyze_property([document], property_type="COLLECTIVE")

        self.assertEqual(result.owner_names, ["지상붕", "지안나"])


class IncompleteFinancialInputTest(unittest.TestCase):
    def test_does_not_mark_safe_without_a_house_price(self):
        document = DeedDocument(
            doc_type=DOC_COLLECTIVE,
            inferred_property_type="COLLECTIVE",
            has_daejigwon=True,
        )

        result = analyze_property([document], property_type="COLLECTIVE", deposit=130_000_000)

        self.assertEqual(result.risk_level, "UNKNOWN")

    def test_returns_jeonse_rate_as_a_percentage_of_house_price(self):
        document = DeedDocument(
            doc_type=DOC_COLLECTIVE,
            has_daejigwon=True,
        )

        result = analyze_property(
            [document],
            property_type="COLLECTIVE",
            deposit=120_000_000,
            property_price=200_000_000,
        )

        self.assertEqual(result.jeonse_rate, 60.0)


class MonthlyRentRecoveryTest(unittest.TestCase):
    def test_estimates_monthly_rent_deposit_recovery_after_senior_mortgage(self):
        document = parse_deed_document("""
        【 표 제 부 】 ( 전유부분의 건물의 표시 )
        서울특별시 동대문구 전농동 152-73
        【 갑 구 】 ( 소유권에 관한 사항 )
        1 소유권보존 소유자 임대인
        【 을 구 】 ( 소유권 이외의 권리에 관한 사항 )
        1 근저당권설정 채권최고액 금100,000,000원
        """)

        result = analyze_property(
            [document],
            property_type="COLLECTIVE",
            lease_type="WOLSE",
            deposit=50_000_000,
            property_price=200_000_000,
        )

        self.assertIsNone(result.jeonse_rate)
        self.assertEqual(result.recovery_price_used, 140_000_000)
        self.assertEqual(result.estimated_recoverable_deposit, 40_000_000)
        self.assertEqual(result.deposit_recovery_rate, 80.0)
        self.assertIsNone(result.hug_eligible)
        self.assertIsNone(result.lh_eligible)


class AddressVerificationTest(unittest.TestCase):
    def test_matches_submitted_jibun_address_before_road_address(self):
        document = DeedDocument(
            doc_type=DOC_COLLECTIVE,
            has_daejigwon=True,
            registry_addresses=["전농동 152-73"],
        )

        result = analyze_property(
            [document],
            property_type="COLLECTIVE",
            submitted_address="서울특별시 동대문구 전농동 152-73",
            road_address="서울특별시 동대문구 서울시립대로 112-1",
        )

        self.assertTrue(result.address_matches_submission)
        self.assertEqual(result.address_match_basis, "SUBMITTED_ADDRESS")
        self.assertEqual(result.registry_address, "전농동 152-73")

    def test_falls_back_to_road_address_when_submitted_address_does_not_match(self):
        document = DeedDocument(
            doc_type=DOC_COLLECTIVE,
            has_daejigwon=True,
            registry_addresses=["서울시립대로 112-1"],
        )

        result = analyze_property(
            [document],
            property_type="COLLECTIVE",
            submitted_address="서울특별시 동대문구 전농동 152-73",
            road_address="서울특별시 동대문구 서울시립대로 112-1",
        )

        self.assertTrue(result.address_matches_submission)
        self.assertEqual(result.address_match_basis, "ROAD_ADDRESS")

    def test_does_not_finalize_risk_when_registry_address_does_not_match(self):
        document = DeedDocument(
            doc_type=DOC_COLLECTIVE,
            has_daejigwon=True,
            registry_addresses=["전농동 152-73"],
        )

        result = analyze_property(
            [document],
            property_type="COLLECTIVE",
            submitted_address="서울특별시 동대문구 휘경동 308-89",
            road_address="서울특별시 동대문구 회기로 18길 46",
            deposit=120_000_000,
            property_price=210_000_000,
        )

        self.assertFalse(result.address_matches_submission)
        self.assertEqual(result.address_match_basis, "MISMATCH")
        self.assertEqual(result.risk_level, "UNKNOWN")


class MultiDocumentRegistryTest(unittest.TestCase):
    def test_deduplicates_joint_mortgage_after_each_document_applies_changes(self):
        building_total, building_mortgages = extract_mortgages("""
        1 근저당권설정 2019년10월22일 채권최고액 금884,000,000원
        근저당권자 양주신용협동조합
        공동담보 토지 휘경동 308-89
        1-1 1번근저당권변경 2020년3월10일 채권최고액 금72,000,000원
        """)
        land_total, land_mortgages = extract_mortgages("""
        1 근저당권설정 2019년3월20일 채권최고액 금494,000,000원
        근저당권자 양주신용협동조합
        공동담보 건물 휘경동 308-89
        1-1 1번근저당권변경 2020년3월10일 채권최고액 금72,000,000원
        """)
        self.assertEqual(building_total, 72_000_000)
        self.assertEqual(land_total, 72_000_000)

        total, items = merge_mortgages([
            DeedDocument(doc_type=DOC_BUILDING, mortgage_items=building_mortgages),
            DeedDocument(doc_type=DOC_LAND, mortgage_items=land_mortgages),
        ])

        self.assertEqual(total, 72_000_000)
        self.assertEqual(sum(item["deduplicated"] for item in items), 1)

    def test_extracts_housing_lease_right_and_marks_cancelled_history(self):
        raw_text = """
        【 표 제 부 】 ( 건물의 표시 )
        1 서울특별시 동대문구 휘경동 308-89 단독주택
        【 갑 구 】 ( 소유권에 관한 사항 )
        1 소유권보존 소유자 한재원 700101-1234567
        【 을 구 】 ( 소유권 이외의 권리에 관한 사항 )
        2 주택임차권 2025년10월17일 임차보증금 금120,000,000원
        3 2번주택임차권등기 2025년11월10일
        말소 해제
        4 주택임차권 2026년4월29일 임차보증금 금120,000,000원
        """

        document = parse_deed_document(raw_text)

        self.assertEqual(document.tenant_right_hits, [
            {
                "keyword": "주택임차권",
                "rank": 2,
                "line": "2 주택임차권 2025년10월17일 임차보증금 금120,000,000원",
                "cancelled": True,
                "amount": 120_000_000,
            },
            {
                "keyword": "주택임차권",
                "rank": 4,
                "line": "4 주택임차권 2026년4월29일 임차보증금 금120,000,000원",
                "cancelled": False,
                "amount": 120_000_000,
            },
        ])

    def test_requires_every_joint_collateral_land_parcel(self):
        building_text = """
        【 표 제 부 】 ( 건물의 표시 )
        1 서울특별시 동대문구 전농동 152-73 다가구주택
        【 갑 구 】 ( 소유권에 관한 사항 )
        1 소유권보존 소유자 안재현 700101-1234567
        【 을 구 】 ( 소유권 이외의 권리에 관한 사항 )
        1 근저당권설정 채권최고액 금650,000,000원
        근저당권자 회기휘경새마을금고
        공동담보 토지 서울특별시 동대문구 전농동
        152-19
        토지 서울특별시 동대문구 전농동
        152-73
        토지 서울특별시 동대문구 전농동
        152-74
        """
        land_text = """
        【 표 제 부 】 ( 토지의 표시 )
        1 서울특별시 동대문구 전농동 152-73
        【 갑 구 】 ( 소유권에 관한 사항 )
        1 소유권보존 소유자 안재현 700101-1234567
        【 을 구 】 ( 소유권 이외의 권리에 관한 사항 )
        """

        result = analyze_property([
            parse_deed_document(building_text),
            parse_deed_document(land_text),
        ])

        self.assertEqual(result.analysis_status, "NEEDS_MORE_DOCS")
        self.assertIn("전농동 152-19", result.required_documents_reason)
        self.assertIn("전농동 152-74", result.required_documents_reason)

    def test_does_not_require_land_registry_when_separate_registry_was_cancelled(self):
        pyojebu = """
        ( 대지권의 표시 )
        2 별도등기 있음 1토지(을구 13번, 14번 근저당권설정등기)
        3 2번 별도등기 말소
        """

        self.assertFalse(has_active_separate_land_registry(pyojebu))

    def test_requires_land_registry_when_partial_cancellation_leaves_separate_registry_active(self):
        pyojebu = """
        ( 대지권의 표시 )
        2 별도등기 있음 1토지(을구 13번, 14번 근저당권설정등기)
        3 2번 별도등기 중 일부말소 별도등기 있음 1토지(을구 13번 근저당권설정등기)
        """

        self.assertTrue(has_active_separate_land_registry(pyojebu))


if __name__ == "__main__":
    unittest.main()
