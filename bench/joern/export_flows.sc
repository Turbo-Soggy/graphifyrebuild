// Export Joern data flows in the neutral shape graphify-ext's `inject --joern` reads.
//
//   joern> importCode("/path/to/repo", "repo")
//   joern> :load bench/joern/export_flows.sc
//   joern> exportFlows("flows.json", sources = cpg.call.name("input|get|.*request.*"),
//                      sinks = cpg.call.name("eval|exec|execute|system|popen"), rule = "python-cmdi")
//   $ graphify-ext inject --joern flows.json
//
// Every element of every flow is located (file, line); the first is the source,
// the last the sink. Paths are made repo-relative here so they match graphify's
// `source_file` values. Nothing about the graph is needed at this stage.
import io.shiftleft.semanticcpg.language._
import io.joern.dataflowengineoss.language._
import io.joern.dataflowengineoss.queryengine.EngineContext

def exportFlows(out: String, sources: Iterator[io.shiftleft.codepropertygraph.generated.nodes.CfgNode],
                sinks: Iterator[io.shiftleft.codepropertygraph.generated.nodes.CfgNode],
                rule: String = "joern")(implicit ctx: EngineContext): Unit = {
  val root = cpg.metaData.root.headOption.getOrElse("")
  def rel(f: String) = if (root.nonEmpty && f.startsWith(root)) f.stripPrefix(root).stripPrefix("/").stripPrefix("\\") else f
  val flows = sinks.reachableByFlows(sources).l.map { path =>
    val elems = path.elements.map { e =>
      ujson.Obj(
        "file"   -> rel(e.location.filename),
        "line"   -> e.lineNumber.map(_.intValue).getOrElse(-1),
        "method" -> e.method.fullName,
        "code"   -> e.code.take(200))
    }
    ujson.Obj("rule" -> rule, "path" -> ujson.Arr(elems: _*))
  }
  os.write.over(os.Path(out, os.pwd), ujson.write(ujson.Obj("flows" -> ujson.Arr(flows: _*)), indent = 2))
  println(s"wrote ${flows.size} flow(s) to $out")
}
