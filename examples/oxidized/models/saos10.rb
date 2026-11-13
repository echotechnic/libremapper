# SPDX-License-Identifier: Apache-2.0
# Adapted from https://github.com/ytti/oxidized/blob/master/lib/oxidized/model/saos10.rb.
# Modified to capture LLDP neighbors as typed topology output.
class SAOS10 < Oxidized::Model
  using Refinements

  prompt /^.*[>\$#] /

  cmd :all do |cfg|
    cfg.insert(0, "\n!-------------------------------------------------------------------------------!\n")
    cfg.cut_both
  end

  cmd "show lldp neighbors" do |state|
    state.type = "topology"
    state.name = "lldp-neighbors"
    state
  end

  cmd "show running config"

  cfg :ssh do
    post_login "set session more off"
    pre_logout "exit"
  end
end
