package com.psianalyzer.model

data class ViewMetadata(
    val id: String?,
    val type: String,
    val layoutFile: String? = null,
    val attributes: Map<String, String> = emptyMap()
)

