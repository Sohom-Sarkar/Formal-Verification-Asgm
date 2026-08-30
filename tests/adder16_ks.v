// Kogge-Stone parallel-prefix adder: carries are computed by a logarithmic
// prefix network over (generate, propagate) pairs rather than rippled.
// Level k combines spans of 2^(k-1), so 16 bits need 4 levels.
module adder16(input [15:0] a, input [15:0] b, input cin,
               output [15:0] sum, output cout);
  wire [79:0] G;               // 5 levels x 16 bits
  wire [79:0] P;

  assign G[15:0] = a & b;
  assign P[15:0] = a ^ b;

  genvar k, i;
  generate
    for (k = 1; k <= 4; k = k + 1) begin : lvl
      for (i = 0; i < 16; i = i + 1) begin : pos
        if (i >= (1 << (k-1))) begin
          assign G[16*k + i] = G[16*(k-1) + i]
                             | (P[16*(k-1) + i] & G[16*(k-1) + i - (1 << (k-1))]);
          assign P[16*k + i] = P[16*(k-1) + i]
                             & P[16*(k-1) + i - (1 << (k-1))];
        end else begin
          assign G[16*k + i] = G[16*(k-1) + i];
          assign P[16*k + i] = P[16*(k-1) + i];
        end
      end
    end
  endgenerate

  wire [16:0] c;
  assign c[0] = cin;
  generate
    for (i = 0; i < 16; i = i + 1) begin : carry
      assign c[i+1] = G[64 + i] | (P[64 + i] & cin);
    end
  endgenerate

  assign sum  = P[15:0] ^ c[15:0];
  assign cout = c[16];
endmodule
