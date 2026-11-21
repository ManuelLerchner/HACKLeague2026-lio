import json

from supplier_chaser.domain.models import Confirmation, Draft, POLine, Supplier

QUESTIONS = {
    "en": (
        "Please confirm the total quantity, delivery date and shipping conditions for this order. "
        "State whether this replaces earlier confirmations. This is an information request only; "
        "no revised terms or costs are accepted."
    ),
    "pl": (
        "Prosimy o potwierdzenie całkowitej ilości, daty dostawy i warunków wysyłki. "
        "Prosimy wskazać, czy odpowiedź zastępuje wcześniejsze potwierdzenia. "
        "To wyłącznie zapytanie informacyjne; nie akceptujemy nowych warunków ani kosztów."
    ),
    "ko": (
        "이 주문의 총 수량, 납품일 및 배송 조건을 확인해 주세요. "
        "이전 확인 내용을 대체하는지 명시해 주세요. "
        "정보 확인 요청이며 변경된 조건이나 비용을 수락하지 않습니다."
    ),
    "zh": (
        "请确认此订单的总数量、交货日期和运输条件，并说明是否替代先前的确认。"
        "这仅为信息请求，不接受任何修改后的条款或费用。"
    ),
}


class FixtureLLM:
    model_version = "deterministic-fixture-v1"

    def extract_confirmation(self, text: str) -> Confirmation:
        # Synthetic messages explicitly embed their recorded structured extraction.
        marker = "CONFIRMATION_JSON:"
        if marker not in text:
            raise ValueError("Fixture extractor requires a recorded CONFIRMATION_JSON payload")
        return Confirmation.model_validate(json.loads(text.split(marker, 1)[1]))

    def draft_supplier_question(self, po: POLine, supplier: Supplier, reason: str) -> Draft:
        return Draft(
            subject=f"Confirmation requested: PO {po.po_number} / {po.line_number}",
            body=f"{supplier.name}\n\nPO {po.po_number} / {po.line_number}\n"
            f"Part: {po.part_number}\nOrdered: {po.ordered_qty} {po.unit}\n"
            f"Requested delivery: {po.requested_delivery}\n\n"
            f"{QUESTIONS[supplier.language]}\n\nProcurement",
            language=supplier.language,
        )
