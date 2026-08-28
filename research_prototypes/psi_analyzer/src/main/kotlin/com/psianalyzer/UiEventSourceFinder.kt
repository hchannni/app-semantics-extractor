package com.psianalyzer

import org.jetbrains.kotlin.com.intellij.openapi.project.Project
import org.jetbrains.kotlin.psi.*
import org.jetbrains.kotlin.psi.psiUtil.startOffset
import org.jetbrains.kotlin.resolve.BindingContext

/**
 * UI Event Source 탐지기
 * 
 * 사용자 인터랙션이 발생하는 지점(Source)을 찾습니다.
 * - setOnClickListener
 * - setOnLongClickListener
 * - addTextChangedListener
 * - XML onClick 속성
 * - Composable onClick 파라미터
 */
class UiEventSourceFinder(
    private val project: Project,
    private val bindingContext: BindingContext
) {
    
    data class EventSource(
        val type: EventType,
        val viewId: String?,          // button_add, edit_text_name 등
        val handlerFunction: String,   // onClick { ... } 내부의 함수 호출
        val location: Location,
        val callChain: List<String> = emptyList() // onClick -> viewModel.method() -> ...
    )
    
    data class Location(
        val file: String,
        val line: Int,
        val offset: Int
    )
    
    enum class EventType {
        CLICK,
        LONG_CLICK,
        TEXT_CHANGED,
        ITEM_SELECTED,
        SWIPE,
        CUSTOM
    }
    
    /**
     * 주어진 파일들에서 UI Event Source를 찾습니다.
     */
    fun findEventSources(ktFiles: List<KtFile>): List<EventSource> {
        val sources = mutableListOf<EventSource>()
        
        ktFiles.forEach { file ->
            // 1. setOnClickListener 패턴 찾기
            sources.addAll(findClickListeners(file))
            
            // 2. setOnLongClickListener 찾기
            sources.addAll(findLongClickListeners(file))
            
            // 3. addTextChangedListener 찾기
            sources.addAll(findTextChangedListeners(file))
            
            // 4. 기타 리스너 패턴
            sources.addAll(findOtherListeners(file))
        }
        
        return sources
    }
    
    /**
     * setOnClickListener 패턴 찾기
     * 
     * 예:
     * - button.setOnClickListener { viewModel.doSomething() }
     * - view.setOnClickListener { handleClick() }
     */
    private fun findClickListeners(file: KtFile): List<EventSource> {
        val sources = mutableListOf<EventSource>()
        
        file.accept(object : KtTreeVisitorVoid() {
            override fun visitCallExpression(expression: KtCallExpression) {
                super.visitCallExpression(expression)
                
                val calleeName = expression.calleeExpression?.text
                if (calleeName == "setOnClickListener") {
                    val source = parseClickListener(expression, file)
                    if (source != null) {
                        sources.add(source)
                    }
                }
            }
        })
        
        return sources
    }
    
    /**
     * setOnClickListener 호출을 파싱합니다.
     */
    private fun parseClickListener(
        callExpr: KtCallExpression,
        file: KtFile
    ): EventSource? {
        // View ID 추출: button.setOnClickListener -> "button"
        val viewId = extractViewId(callExpr)
        
        // Lambda 블록 내부의 함수 호출 추출
        val lambdaArg = callExpr.lambdaArguments.firstOrNull()
        val callChain = if (lambdaArg != null) {
            extractCallChain(lambdaArg.getLambdaExpression())
        } else {
            emptyList()
        }
        
        val handlerFunction = callChain.firstOrNull() ?: "anonymous"
        
        return EventSource(
            type = EventType.CLICK,
            viewId = viewId,
            handlerFunction = handlerFunction,
            location = Location(
                file = file.name,
                line = getLineNumber(callExpr, file),
                offset = callExpr.startOffset
            ),
            callChain = callChain
        )
    }
    
    /**
     * View ID 추출
     * 
     * button.setOnClickListener -> "button"
     * findViewById<Button>(R.id.button_add).setOnClickListener -> "button_add"
     */
    private fun extractViewId(callExpr: KtCallExpression): String? {
        // Qualified expression: button.setOnClickListener
        val parent = callExpr.parent
        if (parent is KtDotQualifiedExpression) {
            val receiver = parent.receiverExpression
            
            // findViewById<Button>(R.id.button_add)
            if (receiver is KtCallExpression && receiver.calleeExpression?.text == "findViewById") {
                return extractResourceId(receiver)
            }
            
            // 단순 변수명: button
            if (receiver is KtNameReferenceExpression) {
                return receiver.text
            }
        }
        
        return null
    }
    
    /**
     * findViewById에서 리소스 ID 추출
     * 
     * findViewById<Button>(R.id.button_add) -> "button_add"
     */
    private fun extractResourceId(findViewByIdCall: KtCallExpression): String? {
        val args = findViewByIdCall.valueArguments
        if (args.isNotEmpty()) {
            val argText = args[0].getArgumentExpression()?.text ?: return null
            // R.id.button_add -> button_add
            return argText.substringAfterLast(".")
        }
        return null
    }
    
    /**
     * Lambda 블록 내부의 함수 호출 체인 추출
     * 
     * { viewModel.createAlarm() } -> ["viewModel.createAlarm()"]
     * { 
     *   val data = getData()
     *   viewModel.update(data)
     * } -> ["getData()", "viewModel.update(data)"]
     */
    private fun extractCallChain(lambdaExpr: KtLambdaExpression?): List<String> {
        if (lambdaExpr == null) return emptyList()
        
        val calls = mutableListOf<String>()
        
        lambdaExpr.bodyExpression?.accept(object : KtTreeVisitorVoid() {
            override fun visitCallExpression(expression: KtCallExpression) {
                super.visitCallExpression(expression)
                
                // Full qualified call: viewModel.method()
                val parent = expression.parent
                if (parent is KtDotQualifiedExpression) {
                    calls.add(parent.text)
                } else {
                    calls.add(expression.text)
                }
            }
        })
        
        return calls
    }
    
    /**
     * setOnLongClickListener 찾기
     */
    private fun findLongClickListeners(file: KtFile): List<EventSource> {
        val sources = mutableListOf<EventSource>()
        
        file.accept(object : KtTreeVisitorVoid() {
            override fun visitCallExpression(expression: KtCallExpression) {
                super.visitCallExpression(expression)
                
                if (expression.calleeExpression?.text == "setOnLongClickListener") {
                    val viewId = extractViewId(expression)
                    val callChain = expression.lambdaArguments.firstOrNull()?.let {
                        extractCallChain(it.getLambdaExpression())
                    } ?: emptyList()
                    
                    sources.add(EventSource(
                        type = EventType.LONG_CLICK,
                        viewId = viewId,
                        handlerFunction = callChain.firstOrNull() ?: "anonymous",
                        location = Location(
                            file = file.name,
                            line = getLineNumber(expression, file),
                            offset = expression.startOffset
                        ),
                        callChain = callChain
                    ))
                }
            }
        })
        
        return sources
    }
    
    /**
     * addTextChangedListener 찾기
     */
    private fun findTextChangedListeners(file: KtFile): List<EventSource> {
        val sources = mutableListOf<EventSource>()
        
        file.accept(object : KtTreeVisitorVoid() {
            override fun visitCallExpression(expression: KtCallExpression) {
                super.visitCallExpression(expression)
                
                val calleeName = expression.calleeExpression?.text
                if (calleeName == "addTextChangedListener" || calleeName == "doAfterTextChanged") {
                    val viewId = extractViewId(expression)
                    val callChain = expression.lambdaArguments.firstOrNull()?.let {
                        extractCallChain(it.getLambdaExpression())
                    } ?: emptyList()
                    
                    sources.add(EventSource(
                        type = EventType.TEXT_CHANGED,
                        viewId = viewId,
                        handlerFunction = callChain.firstOrNull() ?: "anonymous",
                        location = Location(
                            file = file.name,
                            line = getLineNumber(expression, file),
                            offset = expression.startOffset
                        ),
                        callChain = callChain
                    ))
                }
            }
        })
        
        return sources
    }
    
    /**
     * 기타 리스너 패턴 찾기
     * - setOnItemSelectedListener
     * - setOnTouchListener
     * - 등등
     */
    private fun findOtherListeners(file: KtFile): List<EventSource> {
        val sources = mutableListOf<EventSource>()
        val listenerPatterns = listOf(
            "setOnItemSelectedListener" to EventType.ITEM_SELECTED,
            "setOnTouchListener" to EventType.CUSTOM,
            "setOnFocusChangeListener" to EventType.CUSTOM
        )
        
        file.accept(object : KtTreeVisitorVoid() {
            override fun visitCallExpression(expression: KtCallExpression) {
                super.visitCallExpression(expression)
                
                val calleeName = expression.calleeExpression?.text
                val matchingPattern = listenerPatterns.find { it.first == calleeName }
                
                if (matchingPattern != null) {
                    val viewId = extractViewId(expression)
                    val callChain = expression.lambdaArguments.firstOrNull()?.let {
                        extractCallChain(it.getLambdaExpression())
                    } ?: emptyList()
                    
                    sources.add(EventSource(
                        type = matchingPattern.second,
                        viewId = viewId,
                        handlerFunction = callChain.firstOrNull() ?: "anonymous",
                        location = Location(
                            file = file.name,
                            line = getLineNumber(expression, file),
                            offset = expression.startOffset
                        ),
                        callChain = callChain
                    ))
                }
            }
        })
        
        return sources
    }
    
    /**
     * PSI element의 줄 번호 계산
     */
    private fun getLineNumber(element: KtElement, file: KtFile): Int {
        val document = file.viewProvider.document ?: return -1
        return document.getLineNumber(element.startOffset) + 1
    }
    
    /**
     * JSON 형식으로 변환
     */
    fun toJson(sources: List<EventSource>): String {
        val json = StringBuilder()
        json.append("{\n")
        json.append("  \"eventSources\": [\n")
        
        sources.forEachIndexed { index, source ->
            json.append("    {\n")
            json.append("      \"type\": \"${source.type}\",\n")
            json.append("      \"viewId\": ${if (source.viewId != null) "\"${source.viewId}\"" else "null"},\n")
            json.append("      \"handlerFunction\": \"${source.handlerFunction}\",\n")
            json.append("      \"location\": {\n")
            json.append("        \"file\": \"${source.location.file}\",\n")
            json.append("        \"line\": ${source.location.line}\n")
            json.append("      },\n")
            json.append("      \"callChain\": [")
            json.append(source.callChain.joinToString(", ") { "\"$it\"" })
            json.append("]\n")
            json.append("    }")
            if (index < sources.size - 1) json.append(",")
            json.append("\n")
        }
        
        json.append("  ]\n")
        json.append("}\n")
        return json.toString()
    }
}

