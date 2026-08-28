package com.psianalyzer

import com.psianalyzer.model.ViewMetadata
import org.jetbrains.kotlin.lexer.KtTokens
import org.jetbrains.kotlin.psi.*
import org.jetbrains.kotlin.psi.psiUtil.getParentOfType
import org.jetbrains.kotlin.psi.psiUtil.getQualifiedExpressionForSelectorOrThis
import org.jetbrains.kotlin.psi.psiUtil.parents
import org.jetbrains.kotlin.psi.psiUtil.startOffset
import org.jetbrains.kotlin.resolve.BindingContext
import org.jetbrains.kotlin.resolve.descriptorUtil.fqNameOrNull
import org.jetbrains.kotlin.types.KotlinType

/**
 * View 선언 및 참조를 전수조사해 UI 인벤토리를 구성하는 수집기.
 * Kotlin PSI와 BindingContext, XML 메타데이터를 조합해 View 인스턴스 정보를 생성한다.
 */
class ViewInventoryCollector(
    private val bindingContext: BindingContext,
    private val layoutMetadata: Map<String, ViewMetadata>
) {

    data class ViewInstance(
        val instanceName: String?,
        val type: String,
        val superTypes: List<String>,
        val androidId: String?,
        val layoutFile: String?,
        val sourceFile: String,
        val containingClass: String?,
        val containingFunction: String?,
        val creationPattern: CreationPattern,
        val line: Int,
        val offset: Int,
        val attributes: Map<String, String>
    )

    enum class CreationPattern {
        FIND_VIEW_BY_ID,
        VIEW_BINDING,
        CONSTRUCTOR,
        UNKNOWN
    }

    /**
     * 프로젝트 내 모든 KtFile을 순회하며 View 관련 호출을 탐색해 ViewInstance 리스트를 만든다.
     * Ver1. 두 가지 케이스만 확인
     *   - 1. 프로퍼티 초기화식 (val digitalClock = find(...))
     *   - 2. 대입식 (view.foo = findViewById(...))
     */
    fun collect(ktFiles: List<KtFile>): List<ViewInstance> {
        val result = mutableListOf<ViewInstance>()
        ktFiles.forEach { file ->
            file.accept(object : KtTreeVisitorVoid() {
                override fun visitProperty(property: KtProperty) {
                    super.visitProperty(property)
                    val initializer = property.initializer ?: return
                    buildInstanceFromExpression(
                        expression = initializer,
                        file = file,
                        instanceName = property.name,
                        declaredTypeHint = property.typeReference?.text
                    )?.let { result.add(it) }
                }

                override fun visitBinaryExpression(expression: KtBinaryExpression) {
                    super.visitBinaryExpression(expression)
                    if (expression.operationToken != KtTokens.EQ) return
                    val rhs = expression.right ?: return
                    val instanceName = extractAssignedName(expression.left)
                    buildInstanceFromExpression(
                        expression = rhs,
                        file = file,
                        instanceName = instanceName
                    )?.let { result.add(it) }
                }
            })
        }
        return result
    }

    /**
     * 주어진 표현식이 View 타입이라면 ViewInstance로 변환한다.
     * BindingContext 타입을 확인해 View인지 판별
     * 프로퍼티 초기화식, 대입식 등 다양한 맥락에서 재사용한다.
     */
    private fun buildInstanceFromExpression(
        expression: KtExpression,
        file: KtFile,
        instanceName: String?,
        declaredTypeHint: String? = null
    ): ViewInstance? {
        // BindingContext 타입을 확인해 View인지 판별
        val kotlinType = bindingContext.getType(expression)
        val viewByType = isViewType(kotlinType)
        val viewByHint = declaredTypeHint?.let { typeNameLooksLikeView(it) } ?: false
        if (!viewByType && !viewByHint) return null

        // 타입 이름과 상위 타입, Android ID, 레이아웃 메타데이터, 생성 패턴을 추출
        val typeName = resolveTypeName(kotlinType) ?: declaredTypeHint ?: "View"
        val superTypes = kotlinType?.let { collectSuperTypes(it) } ?: emptyList()
        val androidId = findAndroidId(expression)
        val metadata = resolveMetadata(androidId)
        val pattern = detectCreationPattern(expression, typeName)

        // ViewInstance 데이터 클래스를 생성
        return buildViewInstance(
            qualifiedExpression = expression.getQualifiedExpressionForSelectorOrThis(),
            typeName = typeName,
            superTypes = superTypes,
            metadata = metadata,
            pattern = pattern,
            instanceName = instanceName ?: metadata?.id,
            file = file,
            androidId = androidId
        )
    }

    /**
     * 공통 필드(위치, 컨텍스트, 메타데이터)를 채워 ViewInstance 데이터 클래스를 생성한다.
     */
    private fun buildViewInstance(
        qualifiedExpression: KtExpression,
        typeName: String,
        superTypes: List<String>,
        metadata: ViewMetadata?,
        pattern: CreationPattern,
        instanceName: String?,
        file: KtFile,
        androidId: String? = null
    ): ViewInstance {
        val locationLine = getLineNumber(qualifiedExpression, file)
        val containingClass = findContainingClassName(qualifiedExpression)
        val containingFunction = findContainingFunctionName(qualifiedExpression)

        return ViewInstance(
            instanceName = instanceName,
            type = typeName,
            superTypes = superTypes,
            androidId = androidId ?: metadata?.id,
            layoutFile = metadata?.layoutFile,
            sourceFile = file.name,
            containingClass = containingClass,
            containingFunction = containingFunction,
            creationPattern = pattern,
            line = locationLine,
            offset = qualifiedExpression.startOffset,
            attributes = metadata?.attributes ?: emptyMap()
        )
    }

    /**
     * KotlinType을 통해 타입 이름을 추출
     */
    private fun resolveTypeName(type: KotlinType?): String? {
        return type?.safeShortName()
    }

    /**
     * KotlinType을 통해 상위 타입 이름을 추출
     */
    private fun collectSuperTypes(type: KotlinType): List<String> {
        val names = mutableListOf<String>()
        type.safeShortName()?.let { names.add(it) }
        type.constructor.supertypes.forEach { superType ->
            superType.safeShortName()?.let { name ->
                if (!names.contains(name)) names.add(name)
            }
        }
        return names
    }

    /**
     * KotlinType을 통해 간단한 타입 이름(short name)을 추출
     * fqName에 대한 예외처리를 통해 안전하게 shortName 추출
     */
    private fun KotlinType.safeShortName(): String? {
        val descriptor = constructor.declarationDescriptor
        val fqName = descriptor?.fqNameOrNull()
        return when {
            fqName == null || fqName.isRoot -> toString()
            else -> fqName.shortName().asString()
        }
    }

    /**
     * findViewById 호출 인자에서 android:id(R.id.foo)나 문자열 ID를 추출
     */
    private fun extractAndroidId(callExpression: KtCallExpression): String? {
        val args = callExpression.valueArguments
        if (args.isEmpty()) return null
        val text = args.first().getArgumentExpression()?.text ?: return null
        return when {
            text.contains("R.id.") -> text.substringAfter("R.id.")
            text.startsWith("\"") -> text.trim('"')
            else -> null
        }
    }

    /**
     * XML 레이아웃 메타데이터 맵에서 주어진 ID에 해당하는 정보를 조회
     */
    private fun resolveMetadata(androidId: String?): ViewMetadata? {
        return androidId?.let { layoutMetadata[it] }
    }

    private fun extractAssignedName(expression: KtExpression?): String? {
        return when (expression) {
            is KtNameReferenceExpression -> expression.getReferencedName()
            is KtDotQualifiedExpression -> expression.text
            else -> null
        }
    }

    /**
     * findViewById 호출에서 android:id(R.id.foo)나 문자열 ID를 추출
     */
    private fun findAndroidId(expression: KtExpression): String? {
        var id: String? = null
        expression.accept(object : KtTreeVisitorVoid() {
            override fun visitCallExpression(expression: KtCallExpression) {
                if (id != null) return
                id = extractAndroidId(expression)
                if (id == null) super.visitCallExpression(expression)
            }
        })
        return id
    }

    private fun detectCreationPattern(expression: KtExpression, typeName: String): CreationPattern {
        if (containsFindViewByIdCall(expression)) return CreationPattern.FIND_VIEW_BY_ID
        if (expression is KtCallExpression && typeNameLooksLikeView(typeName)) {
            return CreationPattern.CONSTRUCTOR
        }
        if (expression is KtBinaryExpressionWithTypeRHS) {
            return detectCreationPattern(expression.left, typeName)
        }
        if (expression is KtDotQualifiedExpression) {
            val selector = expression.selectorExpression
            if (selector != null) {
                return detectCreationPattern(selector, typeName)
            }
        }
        if (expression is KtParenthesizedExpression) {
            val inner = expression.expression
            if (inner != null) {
                return detectCreationPattern(inner, typeName)
            }
        }
        return CreationPattern.UNKNOWN
    }

    /**
     * findViewById 호출이 포함되어 있는지 판별
     */
    private fun containsFindViewByIdCall(expression: KtExpression): Boolean {
        var found = false
        expression.accept(object : KtTreeVisitorVoid() {
            override fun visitCallExpression(expression: KtCallExpression) {
                if (found) return
                val calleeName = expression.calleeExpression?.text
                if (calleeName != null && calleeName in FIND_VIEW_BY_ID_NAMES) {
                    found = true
                    return
                }
                super.visitCallExpression(expression)
            }
        })
        return found
    }

    private val FIND_VIEW_BY_ID_NAMES = setOf(
        "findViewById",
        "requireViewById",
        "findViewByIdOrNull"
    )

    /**
     * KotlinType이 View 계열인지 판별
     */
    private fun isViewType(type: KotlinType?): Boolean {
        if (type == null) return false
        val fqName = type.constructor.declarationDescriptor?.fqNameOrNull()?.asString()
        if (fqName != null) {
            if (fqName == "android.view.View" ||
                fqName.startsWith("android.view.") ||
                fqName.startsWith("android.widget.") ||
                fqName.startsWith("androidx.")
            ) {
                return true
            }
        }
        return type.constructor.supertypes.any { isViewType(it) }
    }

    /**
     * 타입 이름이 View 계열인지 판별
     */
    private fun typeNameLooksLikeView(typeName: String): Boolean {
        return typeEndsWithView(typeName)
    }

    /**
     * PSI 요소가 속한 가장 가까운 클래스 선언명을 얻는다.
     */
    private fun findContainingClassName(element: KtElement): String? {
        return element.parents.filterIsInstance<KtClass>().firstOrNull()?.name
    }

    /**
     * PSI 요소가 속한 함수 이름을 찾는다.
     */
    private fun findContainingFunctionName(element: KtElement): String? {
        return element.parents.filterIsInstance<KtNamedFunction>().firstOrNull()?.name
    }

    /**
     * 문서 객체를 통해 PSI 요소의 시작 줄 번호(1-indexed)를 계산한다.
     */
    private fun getLineNumber(element: KtElement, file: KtFile): Int {
        val document = file.viewProvider.document ?: return -1
        return document.getLineNumber(element.startOffset) + 1
    }

    /**
     * 단순 기준으로 View/ListLayout 계열인지 판별하는 helper.
     */
    private fun typeEndsWithView(typeName: String): Boolean {
        return typeName.endsWith("View") || typeName.endsWith("Layout")
    }

    /**
     * ViewInstance 목록을 사람이 읽기 쉬운 JSON 문자열로 직렬화한다.
     */
    fun toJson(instances: List<ViewInstance>): String {
        fun String.escapeJson(): String =
            this.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")

        fun jsonString(value: String?): String =
            value?.let { "\"${it.escapeJson()}\"" } ?: "null"

        val sb = StringBuilder()
        sb.append("{\n")
        sb.append("  \"views\": [\n")
        instances.forEachIndexed { index, view ->
            sb.append("    {\n")
            sb.append("      \"instanceName\": ${jsonString(view.instanceName)},\n")
            sb.append("      \"type\": \"${view.type.escapeJson()}\",\n")
            sb.append("      \"superTypes\": [${view.superTypes.joinToString(", ") { "\"${it.escapeJson()}\"" }}],\n")
            sb.append("      \"androidId\": ${jsonString(view.androidId)},\n")
            sb.append("      \"layoutFile\": ${jsonString(view.layoutFile)},\n")
            sb.append("      \"sourceFile\": \"${view.sourceFile.escapeJson()}\",\n")
            sb.append("      \"containingClass\": ${jsonString(view.containingClass)},\n")
            sb.append("      \"containingFunction\": ${jsonString(view.containingFunction)},\n")
            sb.append("      \"creationPattern\": \"${view.creationPattern}\",\n")
            sb.append("      \"line\": ${view.line},\n")
            sb.append("      \"offset\": ${view.offset},\n")
            sb.append("      \"attributes\": {\n")
            view.attributes.entries.sortedBy { it.key }.forEachIndexed { attrIndex, entry ->
                sb.append("        \"${entry.key.escapeJson()}\": \"${entry.value.escapeJson()}\"")
                if (attrIndex < view.attributes.size - 1) sb.append(",")
                sb.append("\n")
            }
            sb.append("      }\n")
            sb.append("    }")
            if (index < instances.size - 1) sb.append(",")
            sb.append("\n")
        }
        sb.append("  ]\n")
        sb.append("}\n")
        return sb.toString()
    }
}


