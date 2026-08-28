package com.psianalyzer

import org.jetbrains.kotlin.com.intellij.openapi.project.Project
import org.jetbrains.kotlin.psi.*
import org.jetbrains.kotlin.lexer.KtTokens
import org.jetbrains.kotlin.resolve.BindingContext
import org.jetbrains.kotlin.com.intellij.psi.PsiElement

/**
 * Raw State 추출기
 * 
 * State Hub에서 전달되는 원시 상태(Raw State) 정보를 추출합니다.
 * 타입 구조를 재귀적으로 분석하여 계층을 유지한 채 추출합니다.
 * 
 * - 타입 계층 구조 보존 (Tree 형태)
 * - 순환 참조 방지
 * - 원시/복합/외부 타입 구분
 */
class RawStateExtractor(
    private val project: Project,
    private val bindingContext: BindingContext
) {
    
    /**
     * Raw State 정보
     * State Stream에서 전달되는 원시 상태 정보
     */
    data class RawStateInfo(
        val streamName: String,
        val streamType: String,  // StateFlow, LiveData, etc.
        val rootType: TypeNode,
        val sourceFile: String
    )
    
    /**
     * 타입 노드 - 재귀적 타입 구조 표현
     */
    sealed class TypeNode {
        abstract val name: String
        abstract val typeName: String
        abstract val isNullable: Boolean
    }
    
    /**
     * 원시 타입 노드 (Int, String, Boolean 등)
     */
    data class PrimitiveTypeNode(
        override val name: String,
        override val typeName: String,
        override val isNullable: Boolean,
        val defaultValue: String? = null
    ) : TypeNode()
    
    /**
     * 복합 타입 노드 (클래스, data class 등)
     */
    data class ComplexTypeNode(
        override val name: String,
        override val typeName: String,
        override val isNullable: Boolean,
        val properties: List<TypeNode>,  // 중첩된 프로퍼티들
        val isDataClass: Boolean = false,
        val sourceFile: String
    ) : TypeNode()
    
    /**
     * 외부/알 수 없는 타입 노드
     */
    data class ExternalTypeNode(
        override val name: String,
        override val typeName: String,
        override val isNullable: Boolean,
        val reason: String  // "Circular reference", "Unknown type", etc.
    ) : TypeNode()
    
    /**
     * State Hub에서 식별된 State Stream들로부터 Raw State를 추출합니다.
     */
    fun extractRawStates(
        stateHubs: List<StateHubIdentifier.StateHub>,
        allFiles: List<KtFile>
    ): List<RawStateInfo> {
        val rawStates = mutableListOf<RawStateInfo>()
        
        stateHubs.forEach { hub ->
            hub.stateStreams.forEach { stream ->
                val visited = mutableSetOf<String>()
                
                val rootType = analyzeTypeRecursively(
                    typeName = stream.dataType,
                    propertyName = stream.propertyName,
                    allFiles,
                    visited
                )
                
                rawStates.add(
                    RawStateInfo(
                        streamName = stream.propertyName,
                        streamType = stream.streamType,
                        rootType,
                        sourceFile = stream.file
                    )
                )
            }
        }
        
        return rawStates
    }
    
    /**
     * BindingContext를 활용한 정확한 타입 추론
     * 
     * - 명시적 타입이 없는 computed property도 처리
     * - Nullable 정보 포함
     * - Generic 타입도 정확하게 추출
     * - Getter expression에서 타입 추론 시도
     */
    private fun getTypeFromBindingContext(element: KtElement): String? {
        return try {
            when (element) {
                is KtProperty -> {
                    // 1. 직접적인 타입 추론 시도
                    var type = bindingContext.getType(element)
                    
                    // 2. 타입이 없으면 getter에서 추론 시도
                    if (type == null && element.getter != null) {
                        val getter = element.getter
                        val returnExpression = getter?.bodyExpression
                        if (returnExpression != null) {
                            type = bindingContext.getType(returnExpression)
                            if (type != null) {
                                println("    🔍 Type inferred from getter: ${element.name} -> $type")
                            }
                        }
                    }
                    
                    val result = type?.toString()
                    if (result == null) {
                        println("    ⚠️  BindingContext failed for property: ${element.name}")
                    }
                    result
                }
                is KtParameter -> {
                    val type = bindingContext.getType(element)
                    type?.toString()
                }
                else -> null
            }
        } catch (e: Exception) {
            println("    ⚠️  Exception in getTypeFromBindingContext: ${e.message}")
            null
        }
    }
    
    /**
     * Expression 텍스트로부터 휴리스틱하게 타입 추론
     * BindingContext가 실패한 경우의 마지막 fallback
     */
    private fun inferTypeFromExpression(expression: String): String? {
        return when {
            // Boolean 반환 패턴들
            expression.contains("==") || expression.contains("!=") -> "Boolean"
            expression.contains("&&") || expression.contains("||") -> "Boolean"
            expression.contains("contentEquals(") -> "Boolean"
            expression.contains(">") || expression.contains("<") -> "Boolean"
            expression.contains(">=") || expression.contains("<=") -> "Boolean"
            expression.contains("is ") || expression.contains("!is ") -> "Boolean"
            expression.contains("in ") || expression.contains("!in ") -> "Boolean"
            
            // String 반환 패턴들
            expression.startsWith("\"") && expression.endsWith("\"") -> "String"
            expression.contains(".toString()") -> "String"
            
            // Number 패턴들
            expression.matches(Regex("\\d+")) -> "Int"
            expression.matches(Regex("\\d+\\.\\d+")) -> "Double"
            expression.matches(Regex("\\d+L")) -> "Long"
            expression.matches(Regex("\\d+f")) -> "Float"
            
            else -> null
        }
    }
    
    /**
     * 타입을 재귀적으로 분석하여 TypeNode 생성
     * 
     * - 원시 타입: PrimitiveTypeNode 반환
     * - 복합 타입: ComplexTypeNode 반환 (프로퍼티들 재귀 분석)
     * - 외부 타입: ExternalTypeNode 반환
     * - 순환 참조: ExternalTypeNode 반환 (reason: "Circular reference")
     */
    private fun analyzeTypeRecursively(
        typeName: String,
        propertyName: String,
        allFiles: List<KtFile>,
        visited: MutableSet<String>
    ): TypeNode {
        val cleanTypeName = typeName.removeSuffix("?").trim()
        val isNullable = typeName.endsWith("?")
        
        // 순환 참조 방지
        if (cleanTypeName in visited) {
            println("⚠️  Circular reference detected: $cleanTypeName")
            return ExternalTypeNode(
                name = propertyName,
                typeName = cleanTypeName,
                isNullable = isNullable,
                reason = "Circular reference"
            )
        }
        
        // 원시 타입 또는 표준 라이브러리 타입 -> Leaf Node
        if (isPrimitiveOrStandardType(cleanTypeName)) {
            return PrimitiveTypeNode(
                name = propertyName,
                typeName = cleanTypeName,
                isNullable = isNullable
            )
        }
        
        // 복합 타입: 클래스 정의 찾기
        val ktClass = findClassDefinition(cleanTypeName, allFiles)
        
        if (ktClass == null) {
            println("⚠️  External or unknown type: $cleanTypeName")
            return ExternalTypeNode(
                name = propertyName,
                typeName = cleanTypeName,
                isNullable = isNullable,
                reason = "External library or unknown type"
            )
        }
        
        // 방문 마킹 (순환 참조 방지)
        visited.add(cleanTypeName)
        println("🔍 Analyzing class: $cleanTypeName")
        
        // 클래스의 모든 프로퍼티를 재귀적으로 분석
        val properties = mutableListOf<TypeNode>()
        val processedNames = mutableSetOf<String>()  // 중복 방지
        
        // 1. Primary constructor parameters (val/var로 선언된 프로퍼티)
        ktClass.getPrimaryConstructorParameters()
            .filter { it.hasValOrVar() }  // val/var로 선언된 것만
            .forEach { param ->
                val paramName = param.name ?: return@forEach    // Java의 continue와 같은 역할. 현재 람다 반복만 종료
                if (paramName in processedNames) return@forEach
                processedNames.add(paramName)
                
                // BindingContext 우선 시도 -> 없으면 텍스트 기반
                val paramType = getTypeFromBindingContext(param) 
                    ?: param.typeReference?.text 
                    ?: "Unknown"
                    
                println("  ├─ constructor property: $paramName: $paramType")
                
                val node = analyzeTypeRecursively(
                    typeName = paramType,
                    propertyName = paramName,
                    allFiles = allFiles,
                    visited = visited
                )
                properties.add(node)
            }
        
        // 2. Body에 선언된 프로퍼티들 (getter-only property 포함)
        ktClass.declarations
            .filterIsInstance<KtProperty>()
            .filter { !it.hasModifier(KtTokens.PRIVATE_KEYWORD) }
            .forEach { property ->
                val propName = property.name ?: return@forEach
                if (propName in processedNames) return@forEach
                processedNames.add(propName)
                
                // BindingContext 우선 시도 -> 없으면 텍스트 기반
                var propType = getTypeFromBindingContext(property)
                    ?: property.typeReference?.text
                
                // 타입을 찾지 못한 경우 휴리스틱 타입 추론 시도
                if (propType == null) {
                    val getter = property.getter
                    val bodyExpression = getter?.bodyExpression
                    if (bodyExpression != null) {
                        val expressionText = bodyExpression.text
                        propType = inferTypeFromExpression(expressionText)
                        if (propType != null) {
                            println("  ├─ body property (heuristic): $propName: $propType")
                        }
                    }
                }
                
                if (propType == null) {
                    println("  ⚠️  Skipping property without type: $propName")
                    propType = "Unknown"
                }
                    
                println("  ├─ body property: $propName: $propType")
                
                val node = analyzeTypeRecursively(
                    typeName = propType,
                    propertyName = propName,
                    allFiles = allFiles,
                    visited = visited
                )
                properties.add(node)
            }
        
        return ComplexTypeNode(
            name = propertyName,
            typeName = cleanTypeName,
            isNullable = isNullable,
            properties = properties,
            isDataClass = ktClass.isData(),
            sourceFile = ktClass.containingKtFile.name
        )
    }
    
    /**
     * 클래스 정의 찾기 - data class 여부 무관
     */
    private fun findClassDefinition(
        className: String, 
        files: List<KtFile>
    ): KtClass? {
        // 제네릭 타입 제거: List<Alarm> -> List
        val cleanName = className.substringBefore('<').trim()
        
        files.forEach { file ->
            var foundClass: KtClass? = null
            file.accept(object : KtTreeVisitorVoid() {
                override fun visitClass(klass: KtClass) {
                    super.visitClass(klass)
                    if (klass.name == cleanName) {
                        foundClass = klass
                    }
                }
            })
            if (foundClass != null) return foundClass
        }
        return null
    }
    
    /**
     * 원시 타입 또는 표준 라이브러리 타입 판별
     */
    private fun isPrimitiveOrStandardType(type: String): Boolean {
        return type in setOf(
            "String", "Int", "Long", "Double", "Float",
            "Boolean", "Char", "Byte", "Short",
            "Unit", "Any", "Nothing"
        ) || type.startsWith("kotlin.") || type.startsWith("java.")
    }
    
    /**
     * TypeNode 트리를 평탄화된 Variable 리스트로 변환
     * 
     * StatePredicateRefiner와의 호환성을 위해 제공됩니다.
     */
    data class FlattenedVariable(
        val name: String,
        val type: String,
        val isNullable: Boolean
    )
    
    fun flattenTypeNode(node: TypeNode, prefix: String = ""): List<FlattenedVariable> {
        val fullName = if (prefix.isEmpty()) node.name else "$prefix.${node.name}"
        
        return when (node) {
            is PrimitiveTypeNode -> {
                listOf(
                    FlattenedVariable(
                        name = fullName,
                        type = node.typeName,
                        isNullable = node.isNullable
                    )
                )
            }
            is ExternalTypeNode -> {
                listOf(
                    FlattenedVariable(
                        name = fullName,
                        type = node.typeName,
                        isNullable = node.isNullable
                    )
                )
            }
            is ComplexTypeNode -> {
                node.properties.flatMap { property ->
                    flattenTypeNode(property, fullName)
                }
            }
        }
    }
    
    fun flattenRawState(rawState: RawStateInfo): List<FlattenedVariable> {
        return flattenTypeNode(rawState.rootType, "")
    }
    
    /**
     * Raw State를 JSON 형식으로 변환
     */
    fun toJson(rawStates: List<RawStateInfo>): String {
        val json = StringBuilder()
        json.append("{\n")
        json.append("  \"rawStates\": [\n")
        
        rawStates.forEachIndexed { index, rawState ->
            json.append("    {\n")
            json.append("      \"streamName\": \"${rawState.streamName}\",\n")
            json.append("      \"streamType\": \"${rawState.streamType}\",\n")
            json.append("      \"sourceFile\": \"${rawState.sourceFile}\",\n")
            json.append("      \"rootType\": ")
            appendTypeNode(json, rawState.rootType, "      ")
            json.append("\n    }")
            if (index < rawStates.size - 1) json.append(",")
            json.append("\n")
        }
        
        json.append("  ]\n")
        json.append("}\n")
        return json.toString()
    }
    
    private fun appendTypeNode(json: StringBuilder, node: TypeNode, indent: String) {
        json.append("{\n")
        json.append("$indent  \"name\": \"${node.name}\",\n")
        json.append("$indent  \"typeName\": \"${node.typeName}\",\n")
        json.append("$indent  \"isNullable\": ${node.isNullable},\n")
        
        when (node) {
            is PrimitiveTypeNode -> {
                json.append("$indent  \"nodeType\": \"primitive\"")
                if (node.defaultValue != null) {
                    json.append(",\n$indent  \"defaultValue\": \"${node.defaultValue}\"")
                }
            }
            is ComplexTypeNode -> {
                json.append("$indent  \"nodeType\": \"complex\",\n")
                json.append("$indent  \"isDataClass\": ${node.isDataClass},\n")
                json.append("$indent  \"sourceFile\": \"${node.sourceFile}\",\n")
                json.append("$indent  \"properties\": [\n")
                node.properties.forEachIndexed { i, prop ->
                    json.append("$indent    ")
                    appendTypeNode(json, prop, "$indent    ")
                    if (i < node.properties.size - 1) json.append(",")
                    json.append("\n")
                }
                json.append("$indent  ]")
            }
            is ExternalTypeNode -> {
                json.append("$indent  \"nodeType\": \"external\",\n")
                json.append("$indent  \"reason\": \"${node.reason}\"")
            }
        }
        
        json.append("\n$indent}")
    }
}

