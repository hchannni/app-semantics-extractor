package com.psianalyzer

import com.psianalyzer.model.ViewMetadata
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.w3c.dom.Element

/**
 * XML 레이아웃 파일에서 View 메타데이터(ID, 타입, 속성 등)를 추출해
 * `ViewMetadata` 맵을 구성하는 유틸리티.
 *
 * 이 파서는 단순한 DOM 순회 방식으로 모든 엘리먼트를 방문하며,
 * `<include>` 같은 특수 태그까지도 동일하게 처리해 ID가 있는 요소만 결과에 추가한다.
 */
class XmlLayoutParser {

    /**
     * 다수의 레이아웃 파일을 파싱해 ID를 키로 하는 메타데이터 맵을 만든다.
     * 존재하지 않거나 XML이 아닌 파일은 건너뛰며, 같은 ID가 중복되면 나중 값이 덮어쓴다.
     */
    fun parseLayoutFiles(layoutFiles: List<File>): Map<String, ViewMetadata> {
        val metadata = mutableMapOf<String, ViewMetadata>()
        layoutFiles.forEach { file ->
            if (file.exists() && file.extension == "xml") {
                parseLayoutFile(file)?.let { metadata.putAll(it) }
            }
        }

        return metadata
    }

    /**
     * 단일 레이아웃 파일을 파싱한다.
     *
     * DOM 큐를 이용해 모든 엘리먼트를 순회하고, `android:id`가 있는 요소만 결과에 포함한다.
     * 파싱 실패 시 null을 반환한다.
     */
    private fun parseLayoutFile(file: File): Map<String, ViewMetadata>? {
        val builder = DocumentBuilderFactory.newInstance().newDocumentBuilder() // DOM 파서
        val document = runCatching { builder.parse(file) }.getOrNull() ?: return null   // 파싱 시도
        document.documentElement.normalize()    // 루트 요소를 normalize()로 정리

        val result = mutableMapOf<String, ViewMetadata>()
        val queue = ArrayDeque<Element>()
        queue.add(document.documentElement)

        while (queue.isNotEmpty()) {    // BFS 수행
            val element = queue.removeFirst()
            val id = element.getAndroidAttribute("id")?.substringAfter("/")

            val type = resolveType(element.tagName, element.getAndroidAttribute("class"))
            val attributes = element.attributes
                .asSequence()
                .associate { attr ->
                    attr.nodeName to attr.nodeValue
                }
            
            val metadata = ViewMetadata(
                id, 
                type,
                layoutFile = file.name, 
                attributes
            )

            if (!id.isNullOrBlank()) {
                result[id] = metadata
            }

            element.childNodes.asSequence()
                .filterIsInstance<Element>()
                .forEach { queue.add(it) }
        }

        return result
    }

    /**
     * 태그명과 `android:class` 속성을 바탕으로 View 타입 이름을 결정한다.
     *
     * 명시된 클래스가 있으면 그것을 사용하고, 그렇지 않으면 태그명에서
     * 패키지 접두사를 제거한 단순 클래스명을 반환한다.
     */
    private fun resolveType(tagName: String, explicitClass: String?): String {
        if (!explicitClass.isNullOrBlank()) return explicitClass.substringAfterLast('.')
        return when {
            tagName == "view" -> "View"
            tagName.contains('.') -> tagName.substringAfterLast('.')
            else -> tagName
        }
    }

    /**
     * 레이아웃 메타데이터 맵을 JSON 문자열로 직렬화한다.
     *
     * @param metadata `parseLayoutFiles` 결과로 얻은 ID → ViewMetadata 맵
     * @return 사람이 읽기 쉬운 포맷의 JSON 문자열
     */
    fun toJson(metadata: Map<String, ViewMetadata>): String {
        fun String.escapeJson(): String =
            this.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")

        fun jsonString(value: String?): String =
            value?.let { "\"${it.escapeJson()}\"" } ?: "null"

        val entries = metadata.values.sortedBy { it.id ?: "" }
        val sb = StringBuilder()
        sb.append("{\n")
        sb.append("  \"views\": [\n")
        entries.forEachIndexed { index, view ->
            sb.append("    {\n")
            sb.append("      \"id\": ${jsonString(view.id)},\n")
            sb.append("      \"type\": \"${view.type.escapeJson()}\",\n")
            sb.append("      \"layoutFile\": ${jsonString(view.layoutFile)},\n")
            sb.append("      \"attributes\": {\n")
            view.attributes.entries.sortedBy { it.key }.forEachIndexed { attrIndex, (key, value) ->
                sb.append("        \"${key.escapeJson()}\": \"${value.escapeJson()}\"")
                if (attrIndex < view.attributes.size - 1) sb.append(",")
                sb.append("\n")
            }
            sb.append("      }\n")
            sb.append("    }")
            if (index < entries.size - 1) sb.append(",")
            sb.append("\n")
        }
        sb.append("  ]\n")
        sb.append("}\n")
        return sb.toString()
    }

    /**
     * DOM Element 확장함수 -> Element 인스턴스에서 getAndroidAttribute(...) 를 호출할 수 있게 해준다.
     * android:id처럼 네임스페이스 접두사를 붙인 속성 값을 읽고, 빈 문자열이면 null로
     * E.g., android:id="@+id/foo"에서 @+id/foo 를 받는다.
     */
    private fun Element.getAndroidAttribute(localName: String): String? {
        return getAttribute("android:$localName").ifBlank { null }
    }

    private fun org.w3c.dom.NamedNodeMap.asSequence(): Sequence<org.w3c.dom.Node> = sequence {
        for (i in 0 until length) {
            yield(item(i))
        }
    }

    private fun org.w3c.dom.NodeList.asSequence(): Sequence<org.w3c.dom.Node> = sequence {
        for (i in 0 until length) {
            yield(item(i))
        }
    }
}