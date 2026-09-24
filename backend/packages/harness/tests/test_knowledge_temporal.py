"""
本文件对外提供 Evidence Unit 原子性值、heading reference 与时态 binding 的机械契约测试。

输入:
    Markdown 文档、DocumentInput、CandidateBlock 及正文/heading/document TemporalBinding。

输出:
    可运行断言，覆盖冻结领域值序列化、同文档多版本解析、字段级优先级、原文锚点、
    跨单元/非继承 heading 拒绝和同优先级冲突拒绝。

具体工作流:
    先用生产结构扫描器取得绝对 heading refs，再调用 temporal 纯函数解析；所有测试不调用
    Store、LLM、评级器或网络。

示例:
    python -m pytest backend/packages/harness/tests/test_knowledge_temporal.py -q
"""

import unittest

from caspian.knowledge.chunking import split_structural_blocks
from caspian.knowledge.evidence import (
    CandidateBlock,
    DocumentInput,
    EvidenceUnit,
    EvidenceValidationError,
    SectionReference,
    SourceSpan,
    TemporalBinding,
    UnitBoundary,
)
from caspian.knowledge.temporal import resolve_temporal_metadata, validate_temporal_bindings


class TemporalDomainTests(unittest.TestCase):
    def test_domain_values_validate_atomicity_and_serialize_bindings(self):
        with self.assertRaises(ValueError):
            UnitBoundary(source_span=SourceSpan(start=0, end=1), atomicity="unknown")
        binding = TemporalBinding(
            field="version",
            value="React 19",
            source_kind="content",
            anchor_text="React 19",
            source_span=SourceSpan(start=0, end=8),
        )
        unit = EvidenceUnit(
            chunk_id="chunk",
            document_id="document",
            document_revision_id="revision",
            content="React 19",
            retrieval_text="Version: React 19\nContent:\nReact 19",
            chunk_index=0,
            source_span=SourceSpan(start=0, end=8),
            temporal_bindings=(binding,),
            atomicity="indivisible",
        )
        value = unit.to_store_value()
        self.assertEqual(value["atomicity"], "indivisible")
        self.assertEqual(value["temporal_bindings"][0]["source_span"], {"start": 0, "end": 8})

    def test_same_document_headings_resolve_independent_versions(self):
        content = "# React 18\n\nOld behavior.\n\n# React 19\n\nNew behavior.\n"
        document = DocumentInput(content=content, source="Official", version="document-default")
        blocks = split_structural_blocks(content, "markdown")
        first = resolve_temporal_metadata(content, document, blocks[0])
        second = resolve_temporal_metadata(content, document, blocks[1])
        self.assertEqual((first.version, second.version), ("React 18", "React 19"))
        for block, metadata in zip(blocks, (first, second), strict=True):
            binding = metadata.bindings[0]
            self.assertEqual(binding.source_kind, "heading")
            self.assertEqual(content[binding.source_span.start:binding.source_span.end], binding.anchor_text)
            self.assertEqual(binding.source_span, block.section_refs[-1].source_span)

    def test_content_binding_overrides_heading_and_document(self):
        content = "# React 18\n\nReact 19 behavior.\n"
        block = split_structural_blocks(content, "markdown")[0]
        local = block.content.index("React 19")
        content_binding = TemporalBinding(
            field="version",
            value="React 19",
            source_kind="content",
            anchor_text="React 19",
            source_span=SourceSpan(
                start=block.source_span.start + local,
                end=block.source_span.start + local + len("React 19"),
            ),
        )
        enriched = block.model_copy(update={"temporal_bindings": (content_binding,)})
        resolved = resolve_temporal_metadata(
            content,
            DocumentInput(content=content, source="Official", version="document-default"),
            enriched,
        )
        self.assertEqual(resolved.version, "React 19")
        self.assertEqual(resolved.bindings[0].source_kind, "content")

    def test_invalid_or_foreign_anchor_is_rejected(self):
        document = "# React 19\n\nFact."
        block = split_structural_blocks(document, "markdown")[0]
        outside = TemporalBinding(
            field="version",
            value="React 19",
            source_kind="content",
            anchor_text="React 19",
            source_span=block.section_refs[0].source_span,
        )
        with self.assertRaises(EvidenceValidationError) as caught:
            validate_temporal_bindings(document, block, (outside,))
        self.assertEqual(caught.exception.code, "temporal_anchor_outside_unit")

        foreign = CandidateBlock(
            kind="paragraph",
            content="Fact.",
            source_span=block.source_span,
            section_path=("Other",),
            section_refs=(SectionReference(title="Other", source_span=SourceSpan(start=2, end=7)),),
            structural_index=0,
        )
        heading = TemporalBinding(
            field="version",
            value="React 19",
            source_kind="heading",
            anchor_text="React 19",
            source_span=block.section_refs[0].source_span,
        )
        with self.assertRaises(EvidenceValidationError):
            validate_temporal_bindings(document, foreign, (heading,))

    def test_conflicting_content_versions_are_rejected(self):
        content = "React 18 and React 19"
        block = CandidateBlock(
            kind="paragraph",
            content=content,
            source_span=SourceSpan(start=0, end=len(content)),
            structural_index=0,
            temporal_bindings=(
                TemporalBinding(field="version", value="React 18", source_kind="content", anchor_text="React 18", source_span=SourceSpan(start=0, end=8)),
                TemporalBinding(field="version", value="React 19", source_kind="content", anchor_text="React 19", source_span=SourceSpan(start=13, end=21)),
            ),
        )
        with self.assertRaises(EvidenceValidationError) as caught:
            resolve_temporal_metadata(content, DocumentInput(content=content, source="Official"), block)
        self.assertEqual(caught.exception.code, "temporal_value_conflict")


if __name__ == "__main__":
    unittest.main()
