@main def exec(
  inputPath: String,
  resourceInventoryPath: String,
  viewInstancesOutputPath: String = "view-instances.json",
  canonicalViewInstancesOutputPath: String = "canonical-view-instances.json",
  anchorsOutputPath: String = "view-anchors-v2.json",
  legacyOutputPath: String = "view-anchors.json",
  viewBindingFieldTypesPath: String = ""
): Unit = {
  ViewAnchorV2Pipeline.exec(
    inputPath = inputPath,
    resourceInventoryPath = resourceInventoryPath,
    viewInstancesOutputPath = viewInstancesOutputPath,
    canonicalViewInstancesOutputPath = canonicalViewInstancesOutputPath,
    anchorsOutputPath = anchorsOutputPath,
    legacyOutputPath = legacyOutputPath,
    viewBindingFieldTypesPath = viewBindingFieldTypesPath
  )
}
