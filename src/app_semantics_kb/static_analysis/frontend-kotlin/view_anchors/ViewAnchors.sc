@main def exec(inputPath: String, outputPath: String = "view-anchors.json"): Unit = {
  ViewAnchorsPipeline.exec(inputPath, outputPath)
}

// ./joern \
//   --import /path/to/joern/view_anchors/ViewAnchorContract.sc \
//   --import /path/to/joern/view_anchors/ResourceLookupRules.sc \
//   --import /path/to/joern/view_anchors/ResourceIdCarrierResolver.sc \
//   --import /path/to/joern/view_anchors/WrapperLookupDiscovery.sc \
//   --import /path/to/joern/view_anchors/BindingFieldRules.sc \
//   --import /path/to/joern/view_anchors/ViewAnchorUsageAnalyzer.sc \
//   --import /path/to/joern/view_anchors/ViewAnchorBuilder.sc \
//   --import /path/to/joern/view_anchors/ResourceLookupCollector.sc \
//   --import /path/to/joern/view_anchors/BindingFieldCollector.sc \
//   --import /path/to/joern/view_anchors/ViewAnchorsPipeline.sc \
//   --script /path/to/joern/ViewAnchors.sc \
//   --param inputPath=/path/to/joern/joern-cli/alarmclock.cpg \
//   --param outputPath=/path/to/joern/view-anchors.json
