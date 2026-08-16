import asyncio
import hashlib
import logging
import re
from typing import Dict, List, Optional, Tuple
import numpy as np
from app.config.settings import settings
from ...constants import line_chunk_space_name, symbol_summary_space_name
from ...models.analysis_status import RepoAnalysisType as AnalysisType
from ..codeast.model import FileInfo
from ..codechunk.code_chunk import LineTextChunk
from ..codesummary.batch_summarizer import (
    SymbolBatchSummarizer,
    SymbolSummaryRequest,
)
from ..codesummary.code_summary import CodeSummary
from ..codesummary.model import ContentType
from app.infrastructure.llms import embedding_factory
from app.infrastructure.vector_store import VECTOR_STORE_CONN
from app.utils.common import normalize_path


_TRIVIAL_SYM_NAME = re.compile(r"^(get|set)[A-Z_][A-Za-z0-9_]*$")
_TRIVIAL_GO_ACCESSOR = re.compile(r"^(Get|Set|Is)[A-Z][A-Za-z0-9_]*$")
_TRIVIAL_JAVA_CPP_ACCESSOR = re.compile(r"^(get|set|is)[A-Z][A-Za-z0-9_]*$")
_TRIVIAL_GO_SINGLE_RETURN = re.compile(r"(?ms)^\s*return\s+.+\s*$")
_TRIVIAL_JAVA_CPP_SINGLE_RETURN = re.compile(r"(?ms)^\s*return\s+[^;]+;\s*$")
_TRIVIAL_JAVA_CPP_SINGLE_ASSIGN = re.compile(r"(?ms)^\s*(this\.)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^;]+;\s*$")

# 向量化：大批次时拆成多段并发调用 model.encode，重叠网络/内部 batch 等待（受信号量限制防打爆 API）
_EMBED_PARALLEL_CHUNK_SIZE = 96
_EMBED_MAX_CONCURRENT = 3


class CodeVectorService:
    """代码分析结果向量化与落库：行块向量与符号（函数/类/方法）摘要向量。"""

    @staticmethod
    async def vectorize_and_store_line_chunks(repo_id: str, rel_file_path: str, chunks: List[LineTextChunk]) -> None:
        """将行切片文本批量嵌入向量，按仓与文件路径幂等写入向量库（先删后插）。"""
        try:
            # 规范化文件路径
            rel_file_path = normalize_path(rel_file_path)
            if not chunks:
                return

            # 第一步：过滤和预处理行块
            # 获取最大字符数限制（默认12000字符）
            max_chars = max(1, int(settings.code_analysis_embed_max_chars or 12000))
            kept: List[LineTextChunk] = []  # 保留的行块
            skipped_empty = 0  # 跳过的空行块计数
            skipped_oversize = 0  # 跳过的超长行块计数
            for c in chunks:
                body = str(c.text or "").strip()
                # 跳过空行块
                if not body:
                    skipped_empty += 1
                    continue
                # 跳过超长行块（避免 embedding 模型处理不了）
                if len(body) > max_chars:
                    skipped_oversize += 1
                    logging.warning(
                        "跳过超长行块 embedding repo_id=%s file=%s lines=%s-%s chars=%s max=%s",
                        repo_id,
                        rel_file_path,
                        c.start_line,
                        c.end_line,
                        len(body),
                        max_chars,
                    )
                    continue
                # 保留有效行块
                kept.append(LineTextChunk(c.start_line, c.end_line, body))

            # 记录跳过的空行块
            if skipped_empty:
                logging.warning(
                    "跳过空行块 embedding repo_id=%s file=%s skipped=%s/%s",
                    repo_id,
                    rel_file_path,
                    skipped_empty,
                    len(chunks),
                )

            # 如果没有保留的行块，直接返回（不算失败，只是没有有效数据）
            if not kept:
                return

            # 第二步：批量 embedding
            # 提取文本内容
            texts = [c.text for c in kept]
            # 调用 embedding 模型（如果全部失败会抛出异常）
            vectors = await CodeVectorService._embed_texts_best_effort(texts)

            # 第三步：过滤 embedding 失败的行块
            paired: List[Tuple[LineTextChunk, List[float]]] = []
            for c, vec in zip(kept, vectors):
                # 跳过 embedding 失败的行块（vec 为 None）
                if vec is None:
                    logging.warning(
                        "跳过 embedding 失败行块 repo_id=%s file=%s lines=%s-%s chars=%s",
                        repo_id,
                        rel_file_path,
                        c.start_line,
                        c.end_line,
                        len(c.text),
                    )
                    continue
                paired.append((c, vec))

            # 部分或全部 embedding 失败均视为失败，保留重试机会
            if len(paired) < len(kept):
                error_msg = f"行块 embedding 部分失败 repo_id={repo_id} file={rel_file_path} success={len(paired)}/{len(kept)}"
                logging.error(error_msg)
                raise RuntimeError(error_msg)
                raise RuntimeError(error_msg)

            # 第四步：写入向量库
            # 获取向量维度
            dim = len(paired[0][1])
            vector_field = f"q_{dim}_vec"
            # 生成向量空间名称
            space_name = line_chunk_space_name(repo_id, dim)
            # 创建向量空间（如果不存在）
            await VECTOR_STORE_CONN.create_space(space_name, dim)
            # 删除旧记录（幂等操作：先删后插）
            await VECTOR_STORE_CONN.delete_records(
                space_name,
                {
                    "repo_id": repo_id,
                    "file_path": rel_file_path,
                    "analysis_type": AnalysisType.LINE_CHUNK_VECTOR.value,
                },
            )

            # 第五步：构建向量记录
            records: List[Dict[str, object]] = []
            for idx, (c, vec) in enumerate(paired):
                # 生成稳定的记录 ID（用于幂等更新）
                stable_id = CodeVectorService._build_stable_id(
                    repo_id=repo_id,
                    file_path=rel_file_path,
                    analysis_type=AnalysisType.LINE_CHUNK_VECTOR.value,
                    start_line=c.start_line,
                    end_line=c.end_line,
                    extra=str(idx),
                )
                # 构建记录
                records.append(
                    {
                        "id": stable_id,
                        "repo_id": repo_id,
                        "file_path": rel_file_path,
                        "analysis_type": AnalysisType.LINE_CHUNK_VECTOR.value,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "chunk_index": idx,
                        "content": c.text,
                        vector_field: vec,  # 向量字段名动态生成
                    }
                )

            # 第六步：批量写入向量库
            failed_ids = await VECTOR_STORE_CONN.insert_records(space_name, records)
            # 如果有写入失败的记录，抛出异常
            if failed_ids:
                raise RuntimeError(f"line chunk写入向量失败: {len(failed_ids)}")
        except Exception as e:
            logging.error("line chunk embedding 失败 repo_id=%s file_path=%s error=%s", repo_id, rel_file_path, e)
            raise RuntimeError(f"line chunk embedding 失败 repo_id={repo_id} file_path={rel_file_path} error={e}") from e

    @staticmethod
    async def vectorize_and_store_symbol_summaries(
        repo_id: str,
        rel_file_path: str,
        file_info: Optional[FileInfo],
    ) -> None:
        """基于 AST 文件信息抽取函数/类/方法，经 LLM 摘要后嵌入并写入符号摘要向量空间。"""
        try:
            # 规范化文件路径
            rel_file_path = normalize_path(rel_file_path)
            if not file_info:
                return

            # 第一步：从 AST 信息中提取符号（函数、类、方法）
            symbols: List[Tuple[str, str, int, int, str, ContentType]] = []
            # 归一化语言标识
            language = CodeVectorService._normalize_language(file_info.language)

            # 提取函数
            for fn in file_info.functions or []:
                name = fn.name or ""
                src = (fn.source_code or "").strip()
                # 跳过空源码
                if not src:
                    continue
                # 跳过低信息度符号（如 getter/setter）
                if CodeVectorService._should_skip_symbol(name, src, language):
                    continue
                # 添加到符号列表
                symbols.append(
                    (
                        "function",
                        name,
                        fn.start_line or 1,
                        fn.end_line or max(fn.start_line or 1, 1),
                        src,
                        ContentType.FUNCTION,
                    )
                )

            # 提取类及其方法
            for clz in file_info.classes or []:
                src = (clz.source_code or "").strip()
                # 跳过空源码
                if not src:
                    continue
                # 添加类本身
                symbols.append(
                    (
                        "class",
                        clz.name,
                        clz.start_line or 1,
                        clz.end_line or max(clz.start_line or 1, 1),
                        src,
                        ContentType.CLASS,
                    )
                )
                # 提取类中的方法
                for method in clz.methods or []:
                    mname = method.name or ""
                    msrc = (method.source_code or "").strip()
                    # 跳过空源码
                    if not msrc:
                        continue
                    # 跳过低信息度符号
                    if CodeVectorService._should_skip_symbol(mname, msrc, language):
                        continue
                    # 添加方法（格式：类名.方法名）
                    symbols.append(
                        (
                            "method",
                            f"{clz.name}.{mname}",
                            method.start_line or 1,
                            method.end_line or max(method.start_line or 1, 1),
                            msrc,
                            ContentType.FUNCTION,
                        )
                    )

            # 如果没有提取到符号，直接返回
            if not symbols:
                return

            # 第二步：批量 LLM 摘要
            # 为每个符号生成摘要（batch_size>1 时一轮多符号；失败按批回退单条）
            summaries = await SymbolBatchSummarizer.summarize_many(
                [
                    SymbolSummaryRequest(source=src, content_type=ct, name=name)
                    for _, name, _, _, src, ct in symbols
                ],
            )

            # 第三步：过滤和预处理符号
            kept_symbols: List[Tuple[str, str, int, int, str, ContentType]] = []
            raw_summaries: List[str] = []
            texts: List[str] = []

            for i, s in enumerate(summaries):
                # 获取摘要文本
                t = (s or "").strip()
                # 如果摘要为空，使用回退摘要（从源码生成）
                if not t:
                    t = CodeVectorService._fallback_summary_from_source(symbols[i][4], symbols[i][5])
                t = (t or "").strip()
                # 如果回退摘要也为空，跳过该符号
                if not t:
                    logging.warning(
                        "跳过空摘要符号 embedding repo_id=%s file=%s symbol=%s",
                        repo_id,
                        rel_file_path,
                        symbols[i][1],
                    )
                    continue

                # 构建 embedding 文本（包含文件路径、符号类型、符号名、摘要）
                kind, name, _, _, _, _ = symbols[i]
                embed_text = CodeVectorService.build_symbol_embed_text(
                    file_path=rel_file_path,
                    symbol_kind=kind,
                    symbol_name=name,
                    summary=t,
                ).strip()
                # 跳过空 embedding 文本
                if not embed_text:
                    logging.warning(
                        "跳过空 embedding 文本 repo_id=%s file=%s symbol=%s",
                        repo_id,
                        rel_file_path,
                        name,
                    )
                    continue

                # 跳过超长 embedding 文本
                max_chars = max(1, int(settings.code_analysis_embed_max_chars or 12000))
                if len(embed_text) > max_chars:
                    logging.warning(
                        "跳过超长符号摘要 embedding repo_id=%s file=%s symbol=%s chars=%s max=%s",
                        repo_id,
                        rel_file_path,
                        name,
                        len(embed_text),
                        max_chars,
                    )
                    continue

                # 保留有效符号
                kept_symbols.append(symbols[i])
                raw_summaries.append(t)
                texts.append(embed_text)

            # 如果没有有效的 embedding 文本，直接返回
            if not texts:
                return
            symbols = kept_symbols

            # 第四步：批量 embedding
            # 调用 embedding 模型（如果全部失败会抛出异常）
            vectors = await CodeVectorService._embed_texts_best_effort(texts)

            # 第五步：过滤 embedding 失败的符号
            paired_sym: List[Tuple[Tuple[str, str, int, int, str, ContentType], str, List[float]]] = []
            for item, summary, vec in zip(symbols, raw_summaries, vectors):
                # 跳过 embedding 失败的符号（vec 为 None）
                if vec is None:
                    logging.warning(
                        "跳过 embedding 失败符号 repo_id=%s file=%s symbol=%s",
                        repo_id,
                        rel_file_path,
                        item[1],
                    )
                    continue
                paired_sym.append((item, summary, vec))

            # 部分或全部 embedding 失败均视为失败，保留重试机会
            if len(paired_sym) < len(symbols):
                error_msg = f"符号摘要 embedding 部分失败 repo_id={repo_id} file={rel_file_path} success={len(paired_sym)}/{len(symbols)}"
                logging.error(error_msg)
                raise RuntimeError(error_msg)

            # 第六步：写入向量库
            # 获取向量维度
            dim = len(paired_sym[0][2])
            vector_field = f"q_{dim}_vec"
            # 生成向量空间名称
            space_name = symbol_summary_space_name(repo_id, dim)
            # 创建向量空间（如果不存在）
            await VECTOR_STORE_CONN.create_space(space_name, dim)
            # 删除旧记录（幂等操作：先删后插）
            await VECTOR_STORE_CONN.delete_records(
                space_name,
                {
                    "repo_id": repo_id,
                    "file_path": rel_file_path,
                    "analysis_type": AnalysisType.SYMBOL_SUMMARY_VECTOR.value,
                },
            )

            # 第七步：构建向量记录
            records: List[Dict[str, object]] = []
            for idx, (item, summary, vec) in enumerate(paired_sym):
                symbol_kind, symbol_name, start_line, end_line, _, _ = item
                # 生成稳定的记录 ID（用于幂等更新）
                stable_id = CodeVectorService._build_stable_id(
                    repo_id=repo_id,
                    file_path=rel_file_path,
                    analysis_type=AnalysisType.SYMBOL_SUMMARY_VECTOR.value,
                    start_line=start_line,
                    end_line=end_line,
                    extra=f"{symbol_kind}:{symbol_name}:{idx}",
                )
                # 构建记录
                records.append(
                    {
                        "id": stable_id,
                        "repo_id": repo_id,
                        "file_path": rel_file_path,
                        "analysis_type": AnalysisType.SYMBOL_SUMMARY_VECTOR.value,
                        "symbol_kind": symbol_kind,
                        "symbol_name": symbol_name,
                        "start_line": start_line,
                        "end_line": end_line,
                        "summary": summary,
                        vector_field: vec,  # 向量字段名动态生成
                    }
                )

            # 第八步：批量写入向量库
            failed_ids = await VECTOR_STORE_CONN.insert_records(space_name, records)
            # 如果有写入失败的记录，抛出异常
            if failed_ids:
                raise RuntimeError(f"symbol summary写入向量失败: {len(failed_ids)}")
        except Exception as e:
            raise RuntimeError(f"symbol summary embedding 失败 repo_id={repo_id} file_path={rel_file_path} error={e}") from e

    @staticmethod
    def _normalize_language(language: Optional[str]) -> str:
        """归一化语言标识，避免大小写/空值影响过滤规则选择。"""
        return (language or "").strip().lower()

    @staticmethod
    def _non_comment_lines(src:str,language:str) -> List[str]:
        """提取去除空白和常见注释行后的代码行。"""
        lines = [ln for ln in src.splitlines() if ln.strip()]
        out: List[str] = []
        for ln in lines:
            s = ln.strip()
            if language == "python" and s.startswith("#"):
                continue
            if language in {
                "java",
                "go",
                "cpp",
                "c",
                "javascript",
                "typescript",
                "rust",
            } and (
                s.startswith("//")
                or s.startswith("/*")
                or s.startswith("*")
                or s.startswith("*/")
            ):
                continue
            out.append(ln)
        return out

    @staticmethod
    def _should_skip_symbol(name:str,src:str,language:str) -> bool:
        """按语言过滤低信息度符号函数（getter/setter/仅返回或仅赋值的小函数）。"""
        lines = CodeVectorService._non_comment_lines(src,language)
        if len(lines) > 8:
            return False
        body = "\n".join(lines)
        lowered = name.lower()

        if language == "python":
            if _TRIVIAL_SYM_NAME.match(name) or lowered.startswith("get_") or lowered.startswith("set_") or lowered.startswith("is_"):
                if len(lines) <= 4 and "return" in body and body.count("def ") <= 1:
                    return True
            return False

        if language == "go":
            if _TRIVIAL_GO_ACCESSOR.match(name) and len(lines) <= 5:
                non_sig = [it.strip() for it in lines if not it.strip().startswith("func ")]
                if len(non_sig) <= 2:
                    joined = " ".join(non_sig)
                    if _TRIVIAL_GO_SINGLE_RETURN.search(joined) or "=" in joined:
                        return True
            return False

        if language in {"java", "cpp", "c", "javascript", "typescript", "rust"}:
            if _TRIVIAL_JAVA_CPP_ACCESSOR.match(name) and len(lines) <= 7:
                non_sig = [it.strip() for it in lines if "(" not in it or ")" not in it]
                core = [it for it in non_sig if it not in {"{", "}", "};"}]
                if len(core) <= 2:
                    joined = " ".join(core)
                    if _TRIVIAL_JAVA_CPP_SINGLE_RETURN.search(joined) or _TRIVIAL_JAVA_CPP_SINGLE_ASSIGN.search(
                        joined
                    ):
                        return True
            return False

        return False

    @staticmethod
    def build_symbol_embed_text(
        *,
        file_path: str,
        symbol_kind: str,
        symbol_name: str,
        summary: str,
    ) -> str:
        """符号向量入库文本：文件路径 + 符号 + 摘要（检索友好靠摘要「检索词」，不做路径切词）。"""
        fp = (file_path or "").replace("\\", "/").strip()
        name = (symbol_name or "").strip()
        parts = [
            f"文件: {fp}" if fp else "",
            f"符号: {symbol_kind} {name}".strip(),
            (summary or "").strip(),
        ]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _fallback_summary_from_source(source_code: str, ct: ContentType) -> str:
        """LLM 摘要为空时，委托 CodeSummary 确定性回退。"""
        return CodeSummary.fallback_summary(source_code, ct)

    @staticmethod
    def _dedupe_texts(texts: List[str]) -> Tuple[List[str], List[int]]:
        """按全文去重，避免相同 chunk 文本重复调用 embedding。
        返回值
          unique: 去重后的文本列表
          index_map: 原文本列表中每个元素在 unique 中的索引，key 为文本，value 为其在 unique 中的索引
        """
        key_to_idx: Dict[str, int] = {}
        unique: List[str] = []
        index_map: List[int] = []
        for text in texts:
            if text not in key_to_idx:
                key_to_idx[text] = len(unique)  # 记录text在 unique 中的索引
                unique.append(text) 
            index_map.append(key_to_idx[text])  # 下标为原texts列表中的索引，值为text在unique中的索引，用于后续还原顺序
        return unique, index_map

    @staticmethod
    async def _encode_one_batch(model: object, batch: List[str]) -> List[List[float]]:
        """单次 model.encode，输出与 batch 等长的向量列表。"""
        try:
            encode = getattr(model, "encode")
            vectors, _ = await encode(batch)
            if vectors is None:
                return []
            arr = np.asarray(vectors)
            if arr.size == 0:
                return []
            if arr.ndim == 1:
                return [arr.tolist()]
            return [arr[i].tolist() for i in range(arr.shape[0])]
        except Exception as e:
            raise RuntimeError(f"embedding 失败 batch={batch} error={e}") from e

    @staticmethod
    async def _encode_unique_texts(model: object, unique: List[str]) -> List[List[float]]:
        """对去重后的文本列表编码；过长时按块并发 encode（每块仍走各后端的内部 batch/retry）。"""
        if not unique:
            return []

        try:
            if len(unique) <= _EMBED_PARALLEL_CHUNK_SIZE:
                out = await CodeVectorService._encode_one_batch(model, unique)
                if len(out) != len(unique):
                    raise RuntimeError("embedding 返回数量与输入不一致")
                return out

            sem = asyncio.Semaphore(_EMBED_MAX_CONCURRENT)
            chunks = [
                unique[i : i + _EMBED_PARALLEL_CHUNK_SIZE]
                for i in range(0, len(unique), _EMBED_PARALLEL_CHUNK_SIZE)
            ]

            async def run_batch(batch: List[str]) -> List[List[float]]:
                async with sem:
                    part = await CodeVectorService._encode_one_batch(model, batch)
                    if len(part) != len(batch):
                        raise RuntimeError("embedding 返回数量与输入不一致")
                    return part

            parts = await asyncio.gather(*[run_batch(c) for c in chunks])
            merged: List[List[float]] = []
            for p in parts:
                merged.extend(p)
            if len(merged) != len(unique):
                raise RuntimeError("embedding 合并结果与唯一文本数不一致")
            return merged
        
        except Exception as e:
            raise RuntimeError(f"embedding 失败 unique={unique} error={e}") from e

    @staticmethod
    async def _embed_texts(texts: List[str]) -> List[List[float]]:
        """调用全局 embedding：先去重，再分块并发 encode，最后按原顺序展开。"""
        if not texts:
            return []
        try:
            model = embedding_factory.create_model()
            if not model:
                raise RuntimeError("embedding模型创建失败")
            unique, index_map = CodeVectorService._dedupe_texts(texts)
            if len(unique) < len(texts):
                logging.info(
                    "embedding 去重: %s 条 -> %s 条唯一文本，节省 %s 次向量计算",
                    len(texts),
                    len(unique),
                    len(texts) - len(unique),
                )
            raw = await CodeVectorService._encode_unique_texts(model, unique)
            if not raw:
                return []
            return [raw[index] for index in index_map]

        except Exception as e:
            raise RuntimeError(f"embedding 失败 texts={texts} error={e}") from e

    @staticmethod
    async def _embed_texts_best_effort(texts: List[str]) -> List[Optional[List[float]]]:
        """入库用 embedding：批量失败时逐条重试，失败项返回 None。"""
        if not texts:
            return []
        
        # 先批量尝试
        try:
            vectors = await CodeVectorService._embed_texts(texts)
            if len(vectors) != len(texts):
                raise RuntimeError("embedding 返回数量与输入不一致")
            return vectors
        except Exception as e:
            logging.warning("批量 embedding 失败，改为逐条重试: %s", e)
        
        # 逐条重试
        out: List[Optional[List[float]]] = []
        for text in texts:
            try:
                part = await CodeVectorService._embed_texts([text])
                out.append(part[0] if part else None)
            except Exception as e2:
                logging.warning(
                    "跳过单条 embedding 失败 chars=%s error=%s",
                    len(text or ""),
                    e2,
                )
                raise RuntimeError(f"embedding 失败 chars={len(text or '')} error={e2}") from e2
        return out

    @staticmethod
    def _build_stable_id(
        repo_id: str,
        file_path: str,
        analysis_type: str,
        start_line: int,
        end_line: int,
        extra: str = "",
    ) -> str:
        """用仓、路径、分析类型、行号与附加键生成 SHA1 稳定记录 ID，便于幂等更新。"""
        raw = f"{repo_id}|{file_path}|{analysis_type}|{start_line}|{end_line}|{extra}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    async def delete_repo_vector_records(repo_id: str) -> int:
        """按 repo_id 删除整仓向量记录（不依赖具体 file_path）。"""
        model = embedding_factory.create_model()
        if not model:
            return 0
        
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            return 0
        dim = len(vectors[0])


        spaces = [
            line_chunk_space_name(repo_id, dim),
            symbol_summary_space_name(repo_id, dim),
        ]        
        deleted = 0
        for space_name in spaces:
            if not await VECTOR_STORE_CONN.space_exists(space_name):
                continue
            deleted += int(await VECTOR_STORE_CONN.delete_records(space_name, {"repo_id": repo_id}))
        return deleted

    @staticmethod
    async def delete_file_vector_records(repo_id: str, rel_file_path: str) -> int:
        """按 repo_id + file_path 删除指定文件的向量记录。"""
        rel_file_path = normalize_path(rel_file_path)
        model = embedding_factory.create_model()
        if not model:
            return 0

        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            return 0
        dim = len(vectors[0])

        spaces = [
            line_chunk_space_name(repo_id, dim),
            symbol_summary_space_name(repo_id, dim),
        ]
        deleted = 0
        for space_name in spaces:
            if not await VECTOR_STORE_CONN.space_exists(space_name):
                continue
            deleted += int(await VECTOR_STORE_CONN.delete_records(space_name, {"repo_id": repo_id, "file_path": rel_file_path}))
        return deleted