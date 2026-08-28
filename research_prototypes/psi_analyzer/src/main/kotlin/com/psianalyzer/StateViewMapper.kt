package com.psianalyzer

import com.psianalyzer.RawStateExtractor
import com.psianalyzer.ViewInventoryCollector
import org.jetbrains.kotlin.com.intellij.openapi.editor.Document
import org.jetbrains.kotlin.lexer.KtTokens
import org.jetbrains.kotlin.psi.*
import org.jetbrains.kotlin.psi.psiUtil.parents
import org.jetbrains.kotlin.psi.psiUtil.startOffset
import org.jetbrains.kotlin.resolve.BindingContext
import org.jetbrains.kotlin.resolve.descriptorUtil.fqNameOrNull

/**
 * RawState 정보와 View Inventory를 이용해 동일한 코드 문맥에서 사용되는 State와 View를 연결한다.
 *
 * 기존 구현은 state 경로 전체 문자열을 그대로 찾았으나,
 * 실제 UI 코드에서는 대부분 domain object(AlarmValue 등)의 property만 직접 접근한다.
 * 따라서 flattened raw state 정보를 "owner type + property" 단위로 인덱싱하고,
 * PSI에서 해당 property reference를 찾은 뒤 같은 문장/함수에 등장하는 View 표현식과 묶는다.
 */
class StateViewMapper(
    private val bindingContext: BindingContext,
    private val rawStateExtractor: RawStateExtractor
) {

    data class StatePropertyInfo(
        val ownerTypeSimple: String,
        val ownerTypeFqName: String?,
        val propertyName: String,
        val fullPath: String,
        val type: String,
        val isNullable: Boolean
    )

    data class UsageLocation(
        val file: String,
        val className: String?,
        val functionName: String?,
        val line: Int,
        val offset: Int,
        val expressionText: String
    )

    data class StateUsage(
        val stateProperty: StatePropertyInfo,
        val location: UsageLocation,
        val expression: KtExpression
    )

    data class StateViewMapping(
        val statePath: String,
        val ownerTypeSimple: String,
        val ownerTypeFqName: String?,
        val propertyName: String,
        val stateType: String,
        val isNullable: Boolean,
        val usage: UsageLocation,
        val view: ViewInventoryCollector.ViewInstance
    )

    fun analyzeStateViewMappings(
        psiFiles: List<KtFile>,
        rawStates: List<RawStateExtractor.RawStateInfo>,
        viewInstances: List<ViewInventoryCollector.ViewInstance>
    ): List<StateViewMapping> {
        val stateProperties = buildStateProperties(rawStates)
        println("[StateViewMapper] stateProperties=${stateProperties.size}")
        if (stateProperties.isEmpty() || psiFiles.isEmpty() || viewInstances.isEmpty()) return emptyList()

        val propertyIndex = stateProperties.groupBy { it.propertyName }
        println("[StateViewMapper] propertyIndexKeys=${propertyIndex.keys.take(10)}${if (propertyIndex.size > 10) "..." else ""}")
        val usages = findUsages(psiFiles, propertyIndex)
        println("[StateViewMapper] usages=${usages.size}")
        if (usages.isEmpty()) return emptyList()

        val viewIndex = ViewContextIndex(viewInstances)

        val mappings = mutableListOf<StateViewMapping>()
        usages.forEach { usage ->
            println(
                "[StateViewMapper] usage state=${usage.stateProperty.fullPath} owner=${usage.stateProperty.ownerTypeFqName} " +
                        "function=${usage.location.functionName} expr=${usage.location.expressionText}"
            )
            val candidateViews = viewIndex.findMatchingViews(usage.location)
            println("[StateViewMapper]   candidateViews=${candidateViews.size}")
            candidateViews.forEach { view ->
                mappings.add(
                    StateViewMapping(
                        statePath = usage.stateProperty.fullPath,
                        ownerTypeSimple = usage.stateProperty.ownerTypeSimple,
                        ownerTypeFqName = usage.stateProperty.ownerTypeFqName,
                        propertyName = usage.stateProperty.propertyName,
                        stateType = usage.stateProperty.type,
                        isNullable = usage.stateProperty.isNullable,
                        usage = usage.location,
                        view = view
                    )
                )
            }
        }
        return mappings
    }

    fun toJson(mappings: List<StateViewMapping>): String {
        fun String.escapeJson(): String =
            this.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")

        fun jsonString(value: String?): String =
            value?.let { "\"${it.escapeJson()}\"" } ?: "null"

        val sb = StringBuilder()
        sb.append("{\n")
        sb.append("  \"stateViewMappings\": [\n")
        mappings.forEachIndexed { index, mapping ->
            val usage = mapping.usage
            val view = mapping.view
            sb.append("    {\n")
            sb.append("      \"statePath\": \"${mapping.statePath.escapeJson()}\",\n")
            sb.append("      \"ownerTypeSimple\": \"${mapping.ownerTypeSimple.escapeJson()}\",\n")
            sb.append("      \"ownerTypeFqName\": ${jsonString(mapping.ownerTypeFqName)},\n")
            sb.append("      \"propertyName\": \"${mapping.propertyName.escapeJson()}\",\n")
            sb.append("      \"stateType\": \"${mapping.stateType.escapeJson()}\",\n")
            sb.append("      \"isNullable\": ${mapping.isNullable},\n")
            sb.append("      \"usage\": {\n")
            sb.append("        \"file\": \"${usage.file.escapeJson()}\",\n")
            sb.append("        \"className\": ${jsonString(usage.className)},\n")
            sb.append("        \"functionName\": ${jsonString(usage.functionName)},\n")
            sb.append("        \"line\": ${usage.line},\n")
            sb.append("        \"offset\": ${usage.offset},\n")
            sb.append("        \"expressionText\": \"${usage.expressionText.escapeJson()}\"\n")
            sb.append("      },\n")
            sb.append("      \"view\": {\n")
            sb.append("        \"instanceName\": ${jsonString(view.instanceName)},\n")
            sb.append("        \"type\": \"${view.type.escapeJson()}\",\n")
            sb.append("        \"androidId\": ${jsonString(view.androidId)},\n")
            sb.append("        \"sourceFile\": \"${view.sourceFile.escapeJson()}\",\n")
            sb.append("        \"containingClass\": ${jsonString(view.containingClass)},\n")
            sb.append("        \"containingFunction\": ${jsonString(view.containingFunction)},\n")
            sb.append("        \"creationPattern\": \"${view.creationPattern}\",\n")
            sb.append("        \"layoutFile\": ${jsonString(view.layoutFile)}\n")
            sb.append("      }\n")
            sb.append("    }")
            if (index < mappings.size - 1) sb.append(",")
            sb.append("\n")
        }
        sb.append("  ]\n")
        sb.append("}\n")
        return sb.toString()
    }

    private fun buildStateProperties(
        rawStates: List<RawStateExtractor.RawStateInfo>
    ): List<StatePropertyInfo> {
        val properties = mutableListOf<StatePropertyInfo>()
        rawStates.forEach { raw ->
            val flattened = rawStateExtractor.flattenRawState(raw)
            flattened.forEach { variable ->
                val segments = variable.name.split(".")
                if (segments.isEmpty()) return@forEach
                val propertyName = segments.last()
                val ownerSegments = segments
                    .dropLast(1)
                    .dropWhile { it == raw.streamName }
                val ownerType = findOwnerType(ownerSegments, raw.rootType) ?: raw.rootType.typeName
                properties.add(
                    StatePropertyInfo(
                        ownerTypeSimple = ownerType.substringAfterLast('.'),
                        ownerTypeFqName = ownerType,
                        propertyName = propertyName,
                        fullPath = variable.name,
                        type = variable.type,
                        isNullable = variable.isNullable
                    )
                )
            }
        }
        return properties
    }

    private fun findOwnerType(
        segments: List<String>,
        rootType: RawStateExtractor.TypeNode
    ): String? {
        var current: RawStateExtractor.TypeNode? = rootType
        for (segment in segments) {
            val complex = current as? RawStateExtractor.ComplexTypeNode ?: return null
            current = complex.properties.firstOrNull { it.name == segment }
        }

        return when (val node = current) {
            is RawStateExtractor.ComplexTypeNode -> node.typeName
            is RawStateExtractor.PrimitiveTypeNode -> node.typeName
            is RawStateExtractor.ExternalTypeNode -> node.typeName
            else -> null
        }
    }

    private fun findUsages(
        psiFiles: List<KtFile>,
        propertyIndex: Map<String, List<StatePropertyInfo>>
    ): List<StateUsage> {
        val usages = mutableListOf<StateUsage>()
        psiFiles.forEach { file ->
            val document = file.viewProvider.document
            file.accept(object : KtTreeVisitorVoid() {
                override fun visitDotQualifiedExpression(expression: KtDotQualifiedExpression) {
                    super.visitDotQualifiedExpression(expression)
                    processExpression(expression, file, document, propertyIndex)?.let { usages.add(it) }
                }

                override fun visitSafeQualifiedExpression(expression: KtSafeQualifiedExpression) {
                    super.visitSafeQualifiedExpression(expression)
                    processExpression(expression, file, document, propertyIndex)?.let { usages.add(it) }
                }

                override fun visitBinaryExpression(expression: KtBinaryExpression) {
                    super.visitBinaryExpression(expression)
                    processBinaryExpression(expression, file, document, propertyIndex)?.let { usages.add(it) }
                }
            })
            if (usages.isNotEmpty()) {
                println("[StateViewMapper] file=${file.name} usages+=${usages.size}")
            }
        }
        return usages
    }

    private fun processBinaryExpression(
        expression: KtBinaryExpression,
        file: KtFile,
        document: Document?,
        propertyIndex: Map<String, List<StatePropertyInfo>>
    ): StateUsage? {
        if (expression.operationToken != KtTokens.EQ && expression.operationToken != KtTokens.PLUSEQ) return null
        val rhs = expression.right ?: return null
        return processExpression(rhs, file, document, propertyIndex)
    }

    private fun processExpression(
        expression: KtExpression,
        file: KtFile,
        document: Document?,
        propertyIndex: Map<String, List<StatePropertyInfo>>
    ): StateUsage? {
        val lastName = expression.findLastNameExpression()?.getReferencedName() ?: return null
        val candidates = propertyIndex[lastName] ?: return null

        val matched = candidates.firstOrNull { candidate ->
            matchesProperty(expression, candidate)
        } ?: return null
        println("[StateViewMapper] matched property=${matched.fullPath} in expr=${expression.text}")

        val className = findContainingClassName(expression)
        val functionName = findContainingFunctionName(expression)
        val line = document?.getLineNumber(expression.startOffset)?.plus(1) ?: -1

        return StateUsage(
            stateProperty = matched,
            location = UsageLocation(
                file = file.name,
                className = className,
                functionName = functionName,
                line = line,
                offset = expression.startOffset,
                expressionText = expression.text
            ),
            expression = expression
        )
    }

    private fun matchesProperty(expression: KtExpression, candidate: StatePropertyInfo): Boolean {
        val nameExpression = expression.findLastNameExpression() ?: return false
        if (nameExpression.getReferencedName() != candidate.propertyName) return false

        val referencedDescriptor = bindingContext[BindingContext.REFERENCE_TARGET, nameExpression]
        val referencedOwnerDescriptor = referencedDescriptor?.containingDeclaration
        val referencedOwnerFqName = referencedOwnerDescriptor?.fqNameOrNull()?.asString()
        val referencedOwnerSimple = referencedOwnerDescriptor?.name?.asString()
        if (matchesOwner(candidate, referencedOwnerFqName, referencedOwnerSimple)) return true

        val qualifier = (expression as? KtQualifiedExpression)?.receiverExpression
            ?: (expression.parent as? KtQualifiedExpression)?.receiverExpression

        if (qualifier != null) {
            val qualifierType = bindingContext.getType(qualifier)
            val qualifierDescriptor = qualifierType?.constructor?.declarationDescriptor
            val qualifierFqName = qualifierDescriptor?.fqNameOrNull()?.asString()
            val qualifierSimple = qualifierDescriptor?.name?.asString()
            if (matchesOwner(candidate, qualifierFqName, qualifierSimple)) {
                return true
            }
        }

        return false
    }

    private fun matchesOwner(
        candidate: StatePropertyInfo,
        fqName: String?,
        simpleName: String?
    ): Boolean {
        candidate.ownerTypeFqName?.let { targetFq ->
            if (fqName == targetFq) return true
        }
        return simpleName == candidate.ownerTypeSimple
    }

    private fun KtExpression.findLastNameExpression(): KtNameReferenceExpression? {
        var current: KtExpression = this
        while (true) {
            current = when (current) {
                is KtDotQualifiedExpression -> current.selectorExpression ?: break
                is KtSafeQualifiedExpression -> current.selectorExpression ?: break
                is KtCallExpression -> current.calleeExpression ?: break
                is KtParenthesizedExpression -> current.expression ?: break
                else -> break
            }
        }
        return current as? KtNameReferenceExpression
    }

    private fun findContainingClassName(element: KtElement): String? {
        return element.parents.filterIsInstance<KtClass>().firstOrNull()?.name
    }

    private fun findContainingFunctionName(element: KtElement): String? {
        return element.parents.filterIsInstance<KtNamedFunction>().firstOrNull()?.name
    }

    private data class ContextKey(
        val file: String,
        val className: String?,
        val functionName: String?
    )

    private class ViewContextIndex(
        viewInstances: List<ViewInventoryCollector.ViewInstance>
    ) {
        private val functionLevel = mutableMapOf<ContextKey, MutableList<ViewInventoryCollector.ViewInstance>>()
        private val classLevel = mutableMapOf<ContextKey, MutableList<ViewInventoryCollector.ViewInstance>>()
        private val fileLevel = mutableMapOf<ContextKey, MutableList<ViewInventoryCollector.ViewInstance>>()

        init {
            viewInstances.forEach { view ->
                val funcKey = ContextKey(view.sourceFile, view.containingClass, view.containingFunction)
                val classKey = ContextKey(view.sourceFile, view.containingClass, null)
                val fileKey = ContextKey(view.sourceFile, null, null)

                functionLevel.getOrPut(funcKey) { mutableListOf() }.add(view)
                classLevel.getOrPut(classKey) { mutableListOf() }.add(view)
                fileLevel.getOrPut(fileKey) { mutableListOf() }.add(view)
            }
        }

        fun findMatchingViews(location: UsageLocation): List<ViewInventoryCollector.ViewInstance> {
            val results = linkedSetOf<ViewInventoryCollector.ViewInstance>()
            val funcKey = ContextKey(location.file, location.className, location.functionName)
            val classKey = ContextKey(location.file, location.className, null)
            val fileKey = ContextKey(location.file, null, null)

            functionLevel[funcKey]?.let { results.addAll(it) }
            classLevel[classKey]?.let { results.addAll(it) }
            fileLevel[fileKey]?.let { results.addAll(it) }

            return results.toList()
        }
    }
}


