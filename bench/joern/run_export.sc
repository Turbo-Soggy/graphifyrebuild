// Non-interactive driver for export_flows.sc.
//
//   joern.bat --script bench/joern/run_export.sc ^
//       --param repo=C:/path/to/repo --param out=flows.json ^
//       --param sources="read_query_param|read_request_body|read_header" ^
//       --param sinks="run_sql|run_shell|render_html" --param rule=corpus-taint
//
// `sources` / `sinks` are regexes matched against call names (the function being
// called). The CPG is built by the language frontend Joern picks from the tree
// (pysrc2cpg for Python, jssrc2cpg for JavaScript, ...). Output is the neutral
// {"flows": [{"rule", "path": [{"file","line","method","code"}]}]} shape that
// `graphify-ext inject --joern` reads; paths are made repo-relative.
import io.shiftleft.semanticcpg.language._
import io.joern.dataflowengineoss.language._
import io.joern.dataflowengineoss.queryengine.EngineContext
import io.joern.dataflowengineoss.semanticsloader.FlowSemantic

@main def exec(repo: String, out: String, sources: String, sinks: String,
               rule: String = "joern"): Unit = {
  importCode(repo, "fixctx")
  implicit val ctx: EngineContext = EngineContext()
  val root = repo.replace('\\', '/').stripSuffix("/")
  def rel(f: String): String = {
    val n = f.replace('\\', '/')
    if (n.startsWith(root)) n.stripPrefix(root).stripPrefix("/") else n
  }
  val src = cpg.call.name(sources).argument ++ cpg.call.name(sources)
  val snk = cpg.call.name(sinks).argument
  val flows = snk.reachableByFlows(src).l.map { path =>
    val elems = path.elements.map { e =>
      ujson.Obj(
        "file"   -> rel(e.location.filename),
        "line"   -> e.lineNumber.map(_.intValue).getOrElse(-1),
        "method" -> e.location.methodFullName,
        "code"   -> e.code.take(200))
    }
    ujson.Obj("rule" -> rule, "path" -> ujson.Arr(elems: _*))
  }
  os.write.over(os.Path(out, os.pwd), ujson.write(ujson.Obj("flows" -> ujson.Arr(flows: _*)), indent = 2))
  println(s"wrote ${flows.size} flow(s) to $out")
}
