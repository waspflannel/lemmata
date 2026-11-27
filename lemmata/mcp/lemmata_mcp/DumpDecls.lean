/-
Dump every public declaration reachable from a root module as JSON lines:
{"n": name, "k": kind, "m": module, "t": pretty-printed type}.

  lake env lean --run DumpDecls.lean Mathlib > decls.jsonl
-/
import Lean
open Lean Meta

def kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem"
  | .defnInfo _ => "def"
  | .axiomInfo _ => "axiom"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quot"

def skip (env : Environment) (name : Name) (info : ConstantInfo) : Bool :=
  name.isInternalDetail || isPrivateName name || info matches .recInfo _
    || isAuxRecursor env name || isNoConfusion env name

unsafe def main (args : List String) : IO Unit := do
  let root := (args.headD "Mathlib").toName
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let env ← importModules #[{ module := root }] {} (loadExts := true)
  let out ← IO.getStdout
  let ctx : Core.Context := { fileName := "<dump>", fileMap := default, maxHeartbeats := 0 }
  for (name, info) in env.constants.map₁.toList do
    if skip env name info then continue
    let some idx := env.getModuleIdxFor? name | continue
    let module := env.header.moduleNames[idx.toNat]!
    let type ← try
        let (fmt, _) ← (MetaM.run' (ppExpr info.type)).toIO ctx { env }
        pure (toString fmt)
      catch _ => pure (toString info.type)
    let line := Json.compress <| Json.mkObj
      [("n", toString name), ("k", kindOf info), ("m", toString module), ("t", type)]
    out.putStrLn line
