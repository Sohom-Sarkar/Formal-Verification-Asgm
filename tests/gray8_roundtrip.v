// Reference: encode to Gray code, then decode back. The composition is the
// identity, but nothing about the circuit says so - proving it requires
// reasoning about the whole XOR-prefix chain.
module gray8(input [7:0] a, output [7:0] y);
  wire [7:0] g;
  assign g = a ^ (a >> 1);            // binary -> Gray

  assign y[7] = g[7];                 // Gray -> binary (XOR prefix)
  assign y[6] = y[7] ^ g[6];
  assign y[5] = y[6] ^ g[5];
  assign y[4] = y[5] ^ g[4];
  assign y[3] = y[4] ^ g[3];
  assign y[2] = y[3] ^ g[2];
  assign y[1] = y[2] ^ g[1];
  assign y[0] = y[1] ^ g[0];
endmodule
